package lan

import (
	"context"
	"io"
	"net"
	"testing"
	"time"
)

func startGatedMismatchProbeServer(t *testing.T) (*net.TCPAddr,
	<-chan struct{}, chan<- struct{}) {
	t.Helper()
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	started := make(chan struct{}, 1)
	release := make(chan struct{})
	go func() {
		conn, acceptErr := listener.Accept()
		if acceptErr != nil {
			return
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
		head := make([]byte, len(capMagic))
		if _, readErr := io.ReadFull(conn, head); readErr != nil ||
			string(head) != capMagic {
			return
		}
		started <- struct{}{}
		<-release
		// A valid signature for another instance produces hostMismatch, the
		// strongest stale result and therefore the best deletion regression.
		_ = RespondProbe(conn, identity,
			"fedcba0987654321fedcba0987654321", "replacement",
			[]string{CapFolder})
	}()
	return listener.Addr().(*net.TCPAddr), started, release
}

func snapshotTestDiscovery() (*Discovery, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	return &Discovery{
		ctx:             ctx,
		cancel:          cancel,
		peers:           make(map[string]*Peer),
		strike:          make(map[string]int),
		verinstr:        make(map[string]bool),
		reported:        make(map[string]bool),
		goodbyeLast:     make(map[string]time.Time),
		goodbyeInFlight: make(map[string]bool),
		verifySem:       make(chan struct{}, maxConcurrentVerifications),
		wake:            make(chan struct{}, 1),
		queryWake:       make(chan struct{}, 1),
		timeout:         2 * time.Second,
		strikes:         probeStrikes,
	}, cancel
}

func TestProbeRoundDoesNotDeleteReplacementEndpoint(t *testing.T) {
	addr, started, release := startGatedMismatchProbeServer(t)
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "same-service"
	original := &Peer{
		InstanceID: vecInstanceID,
		Name:       "old endpoint",
		Host:       addr.IP.String(),
		Hosts:      []string{addr.IP.String()},
		Port:       addr.Port,
	}
	discovery.peers[key] = original

	done := make(chan struct{})
	go func() {
		discovery.probeRound()
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("old endpoint was not probed")
	}
	replacement := &Peer{
		InstanceID: vecInstanceID,
		Name:       "new endpoint",
		Host:       "192.0.2.44",
		Hosts:      []string{"192.0.2.44"},
		Port:       41444,
	}
	discovery.mu.Lock()
	discovery.peers[key] = replacement
	discovery.mu.Unlock()
	close(release)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("probe round did not finish")
	}
	if current := discovery.peers[key]; current != replacement {
		t.Fatalf("stale probe replaced/deleted current peer: %#v", current)
	}
}

func TestGoodbyeDoesNotDeleteReconnectedSameInstance(t *testing.T) {
	addr, started, release := startGatedMismatchProbeServer(t)
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "same-service"
	original := &Peer{
		InstanceID: vecInstanceID,
		Name:       "old endpoint",
		Host:       addr.IP.String(),
		Hosts:      []string{addr.IP.String()},
		Port:       addr.Port,
	}
	discovery.peers[key] = original

	discovery.handleGoodbye(addr.IP.String(), &Announcement{
		InstanceID: vecInstanceID,
		Port:       addr.Port,
		Bye:        true,
	})
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("goodbye verification did not start")
	}
	replacement := &Peer{
		InstanceID: vecInstanceID,
		Name:       "new endpoint",
		Host:       "192.0.2.45",
		Hosts:      []string{"192.0.2.45"},
		Port:       41445,
	}
	discovery.mu.Lock()
	discovery.peers[key] = replacement
	discovery.mu.Unlock()
	close(release)
	discovery.wg.Wait()
	if current := discovery.peers[key]; current != replacement {
		t.Fatalf("old goodbye removed reconnected peer: %#v", current)
	}
}

func TestCrossSourceMergeRefreshesEndpointAndPort(t *testing.T) {
	peerIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, addr := startCloseableProbeServer(t, peerIdentity,
		vecInstanceID, "refreshed endpoint", []string{CapFolder})
	defer listener.Close()
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	discovery.peers["mdns-service"] = &Peer{
		InstanceID:  vecInstanceID,
		Name:        "stale endpoint",
		Host:        "192.0.2.10",
		Hosts:       []string{"192.0.2.10"},
		Port:        41000,
		ServiceName: "mdns-service",
	}

	discovery.verifyCandidate("broadcast|"+vecInstanceID, "",
		[]string{addr.IP.String()}, addr.Port, vecInstanceID)
	discovery.wg.Wait()
	peers := discovery.Peers()
	if len(peers) != 1 {
		t.Fatalf("merged peers = %#v", peers)
	}
	if peers[0].Host != addr.IP.String() || peers[0].Port != addr.Port {
		t.Fatalf("merged endpoint = %s:%d, want %s:%d", peers[0].Host,
			peers[0].Port, addr.IP, addr.Port)
	}
}

func TestUnchangedProbeKeepsEndpointGeneration(t *testing.T) {
	peerIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, addr := startCloseableProbeServer(t, peerIdentity,
		vecInstanceID, "stable endpoint", []string{CapFolder})
	defer listener.Close()
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "stable-service"
	original := &Peer{
		InstanceID:   vecInstanceID,
		Name:         "stable endpoint",
		Host:         addr.IP.String(),
		Hosts:        []string{addr.IP.String()},
		Port:         addr.Port,
		Capabilities: []string{CapFolder},
		PublicKey:    peerIdentity.PublicKey,
		Fingerprint:  peerIdentity.Fingerprint,
		ServiceName:  key,
	}
	discovery.peers[key] = original

	discovery.probeRound()
	if current := discovery.peers[key]; current != original {
		t.Fatalf("unchanged liveness probe replaced endpoint generation: %#v", current)
	}
}

func TestBroadcastBurstSendsConfiguredCount(t *testing.T) {
	remaining := broadcastBurst
	sends := 0
	for remaining > 0 {
		sends++
		var delay time.Duration
		remaining, delay = advanceBroadcastBurst(remaining)
		if remaining > 0 && delay != broadcastBurstGap {
			t.Fatalf("in-burst delay = %v, want %v", delay, broadcastBurstGap)
		}
		if remaining == 0 && delay != broadcastInterval {
			t.Fatalf("post-burst delay = %v, want %v", delay, broadcastInterval)
		}
	}
	if sends != broadcastBurst {
		t.Fatalf("burst sent %d packets, want %d", sends, broadcastBurst)
	}
}
