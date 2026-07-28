package lan

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"os"
	"sort"
	"testing"
	"time"
)

// Live latency measurement for the discovery stack. It needs real multicast
// and broadcast on a real interface, so it stays behind an environment guard
// rather than running in CI: INKHOLE_LIVE_DISCOVERY=1 go test -run Latency.
//
// What it measures is the number the user actually feels — the gap between a
// device joining the network and it appearing in the other device's list.

func randomInstanceID(t *testing.T) string {
	t.Helper()
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(raw)
}

// startLANProbeServer answers WHPC on every interface, which discovery needs
// in order to verify a candidate reached over its LAN address. The listener is
// returned so a caller can close it at the moment it simulates a departure.
func startLANProbeServer(t *testing.T, identity *Identity, instanceID,
	peerName string) (net.Listener, int) {
	t.Helper()
	listener, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func(conn net.Conn) {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
				head := make([]byte, 4)
				if _, err := io.ReadFull(conn, head); err != nil ||
					string(head) != capMagic {
					return
				}
				_ = RespondProbe(conn, identity, instanceID, peerName,
					[]string{"folder-v1", "whe4"})
			}(conn)
		}
	}()
	return listener, listener.Addr().(*net.TCPAddr).Port
}

func startLiveNode(t *testing.T, name, instanceID string,
	onPeers func([]Peer)) (*Discovery, net.Listener) {
	t.Helper()
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, port := startLANProbeServer(t, identity, instanceID, name)
	discovery, err := Start(Config{
		PeerName:     name,
		InstanceID:   instanceID,
		Port:         port,
		Identity:     identity,
		Capabilities: []string{"folder-v1", "whe4"},
		LocalIPs:     LocalIPv4s(),
	}, onPeers, nil)
	if err != nil {
		t.Fatal(err)
	}
	return discovery, listener
}

func TestLiveDiscoveryLatency(t *testing.T) {
	if os.Getenv("INKHOLE_LIVE_DISCOVERY") != "1" {
		t.Skip("set INKHOLE_LIVE_DISCOVERY=1 to measure against the real network")
	}
	if len(LocalIPv4s()) == 0 {
		t.Skip("no routable IPv4 address")
	}

	const rounds = 5
	var discovery, departure []time.Duration
	for round := 0; round < rounds; round++ {
		found := make(chan time.Time, 8)
		lost := make(chan time.Time, 8)
		// Fixed before the observer starts so its callback never races the
		// test goroutine over this value.
		wantID := randomInstanceID(t)
		observer, _ := startLiveNode(t, "观察者", randomInstanceID(t),
			func(peers []Peer) {
				for _, peer := range peers {
					if peer.InstanceID == wantID {
						select {
						case found <- time.Now():
						default:
						}
						return
					}
				}
				select {
				case lost <- time.Now():
				default:
				}
			})

		// Let the observer settle so we time the join, not the startup.
		time.Sleep(1500 * time.Millisecond)
		for len(found) > 0 {
			<-found
		}
		for len(lost) > 0 {
			<-lost
		}

		start := time.Now()
		joiner, joinListener := startLiveNode(t, "加入者", wantID, nil)

		select {
		case at := <-found:
			discovery = append(discovery, at.Sub(start))
		case <-time.After(15 * time.Second):
			joiner.Stop()
			observer.Stop()
			t.Fatalf("round %d: joiner was never discovered", round)
		}

		// Now the other direction: a clean shutdown closes the transfer port
		// and announces departure, exactly as quitting the app does.
		leave := time.Now()
		_ = joinListener.Close()
		joiner.Stop()
		select {
		case at := <-lost:
			departure = append(departure, at.Sub(leave))
		case <-time.After(30 * time.Second):
			observer.Stop()
			t.Fatalf("round %d: joiner never left the list", round)
		}
		observer.Stop()
	}

	report := func(label string, samples []time.Duration) {
		sort.Slice(samples, func(i, j int) bool {
			return samples[i] < samples[j]
		})
		p50 := samples[len(samples)/2]
		p95 := samples[len(samples)*95/100]
		if p95 >= time.Duration(len(samples)) {
			p95 = samples[len(samples)-1]
		}
		lines := make([]string, 0, len(samples))
		for _, sample := range samples {
			lines = append(lines, fmt.Sprintf("%.0fms",
				sample.Seconds()*1000))
		}
		t.Logf("%s: P50=%.0fms P95=%.0fms  样本=%v", label,
			p50.Seconds()*1000, p95.Seconds()*1000, lines)
	}
	report("同网发现", discovery)
	report("正常退出后消失", departure)
}
