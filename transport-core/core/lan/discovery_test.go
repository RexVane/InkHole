package lan

import (
	"io"
	"net"
	"testing"
	"time"
)

// Python reference vectors (p2p._encode_lan_announcement / _service_label).
const (
	pyAnnouncement  = `{"instance_id":"a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8","magic":"inkhole-lan-v1","port":41300,"reply":false,"version":3}`
	pyAnnounceReply = `{"instance_id":"a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8","magic":"inkhole-lan-v1","port":41300,"reply":true,"version":3}`
)

func TestDecodePythonAnnouncement(t *testing.T) {
	decoded := DecodeAnnouncement([]byte(pyAnnouncement))
	if decoded == nil || decoded.InstanceID != vecInstanceID ||
		decoded.Port != 41300 || decoded.Reply {
		t.Fatalf("decode python announcement = %+v", decoded)
	}
	reply := DecodeAnnouncement([]byte(pyAnnounceReply))
	if reply == nil || !reply.Reply {
		t.Fatalf("decode python reply = %+v", reply)
	}
}

func TestAnnouncementRoundTripAndRejects(t *testing.T) {
	encoded, err := EncodeAnnouncement(vecInstanceID, 41300, false)
	if err != nil {
		t.Fatal(err)
	}
	decoded := DecodeAnnouncement(encoded)
	if decoded == nil || decoded.InstanceID != vecInstanceID ||
		decoded.Port != 41300 || decoded.Reply {
		t.Fatalf("round trip = %+v", decoded)
	}
	for _, bad := range []string{
		`{"magic":"other","version":3,"instance_id":"` + vecInstanceID + `","port":1,"reply":false}`,
		`{"magic":"inkhole-lan-v1","version":2,"instance_id":"` + vecInstanceID + `","port":1,"reply":false}`,
		`{"magic":"inkhole-lan-v1","version":3,"instance_id":"short","port":1,"reply":false}`,
		`{"magic":"inkhole-lan-v1","version":3,"instance_id":"` + vecInstanceID + `","port":0,"reply":false}`,
		`not json`,
		``,
	} {
		if got := DecodeAnnouncement([]byte(bad)); got != nil {
			t.Fatalf("accepted invalid announcement %q -> %+v", bad, got)
		}
	}
	if _, err := EncodeAnnouncement("bad", 41300, false); err == nil {
		t.Fatal("encoded invalid instance id")
	}
	if _, err := EncodeAnnouncement(vecInstanceID, 0, false); err == nil {
		t.Fatal("encoded invalid port")
	}
}

func TestServiceLabelMatchesPython(t *testing.T) {
	got := ServiceLabel("墨洞.测试机器名字很长很长很长很长很长很长",
		vecInstanceID)
	if got != "墨洞-测试机器名字很长很长很-a1b2c3d4" {
		t.Fatalf("ServiceLabel long = %q", got)
	}
	if got := ServiceLabel("My.Mac", "fedcba0987654321fedcba0987654321"); got != "My-Mac-fedcba09" {
		t.Fatalf("ServiceLabel short = %q", got)
	}
}

func TestParseTXT(t *testing.T) {
	txt := parseTXT([]string{"peer_name=墨洞-Mac", "whpc=3", "novalue", "a=b=c"})
	if txt["peer_name"] != "墨洞-Mac" || txt["whpc"] != "3" || txt["a"] != "b=c" {
		t.Fatalf("parseTXT = %v", txt)
	}
	if _, ok := txt["novalue"]; ok {
		t.Fatal("entry without '=' should be dropped")
	}
}

// startCloseableProbeServer runs a WHPC responder the test can take down
// mid-flight to exercise eviction.
func startCloseableProbeServer(t *testing.T, identity *Identity,
	instanceID, peerName string, caps []string) (net.Listener, *net.TCPAddr) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
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
				_ = RespondProbe(conn, identity, instanceID, peerName, caps)
			}(conn)
		}
	}()
	return listener, listener.Addr().(*net.TCPAddr)
}

func startTestDiscovery(t *testing.T, updates chan []Peer) *Discovery {
	t.Helper()
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	discovery, err := Start(Config{
		PeerName:         "发现者",
		InstanceID:       "00112233445566770011223344556677",
		Port:             41300,
		Identity:         identity,
		Capabilities:     []string{"folder-v1"},
		DisableMDNS:      true,
		DisableBroadcast: true,
		ProbeInterval:    150 * time.Millisecond,
		ProbeTimeout:     500 * time.Millisecond,
		ProbeStrikes:     2,
	}, func(peers []Peer) {
		select {
		case updates <- peers:
		default:
		}
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(discovery.Stop)
	return discovery
}

func waitForPeerCount(t *testing.T, updates chan []Peer, want int,
	timeout time.Duration) []Peer {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case peers := <-updates:
			if len(peers) == want {
				return peers
			}
		case <-deadline:
			t.Fatalf("peer list never reached %d entries", want)
		}
	}
}

func TestDiscoveryVerifiesAndEvicts(t *testing.T) {
	peerIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, addr := startCloseableProbeServer(t, peerIdentity,
		vecInstanceID, "被发现节点", []string{"folder-v1"})
	updates := make(chan []Peer, 16)
	discovery := startTestDiscovery(t, updates)

	discovery.handleAnnouncement("127.0.0.1", &Announcement{
		InstanceID: vecInstanceID, Port: addr.Port})
	peers := waitForPeerCount(t, updates, 1, 5*time.Second)
	if peers[0].InstanceID != vecInstanceID || peers[0].Name != "被发现节点" {
		t.Fatalf("verified peer = %+v", peers[0])
	}
	if peers[0].Host != "127.0.0.1" || peers[0].Port != addr.Port {
		t.Fatalf("peer endpoint = %s:%d", peers[0].Host, peers[0].Port)
	}
	if peers[0].Fingerprint != peerIdentity.Fingerprint {
		t.Fatal("fingerprint mismatch")
	}
	if len(discovery.Peers()) != 1 {
		t.Fatal("Peers() disagrees with callback")
	}

	// Take the responder down: after ProbeStrikes failed rounds the peer
	// must leave the list.
	_ = listener.Close()
	waitForPeerCount(t, updates, 0, 10*time.Second)
}

func TestDiscoveryDropsUnverifiableCandidate(t *testing.T) {
	// A port with nothing listening: the candidate must never surface.
	closed, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := closed.Addr().(*net.TCPAddr).Port
	_ = closed.Close()

	updates := make(chan []Peer, 4)
	discovery := startTestDiscovery(t, updates)
	discovery.handleAnnouncement("127.0.0.1", &Announcement{
		InstanceID: vecInstanceID, Port: port})
	select {
	case peers := <-updates:
		t.Fatalf("unverifiable candidate surfaced: %+v", peers)
	case <-time.After(1200 * time.Millisecond):
	}
}
