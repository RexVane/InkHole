package lan

import (
	"context"
	"io"
	"net"
	"sync/atomic"
	"testing"
	"time"
)

// gatedProbeServer answers WHPC with a valid signature, but only after the
// test opens the release gate. It lets a test replace map state while a probe
// is provably still in flight, and counts accepts to observe throttling.
type gatedProbeServer struct {
	addr    *net.TCPAddr
	started chan struct{}
	release chan struct{}
	accepts int32
}

func startGatedValidProbeServer(t *testing.T, identity *Identity,
	instanceID, peerName string) *gatedProbeServer {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	server := &gatedProbeServer{
		addr:    listener.Addr().(*net.TCPAddr),
		started: make(chan struct{}, 8),
		release: make(chan struct{}),
	}
	go func() {
		for {
			conn, acceptErr := listener.Accept()
			if acceptErr != nil {
				return
			}
			go func(conn net.Conn) {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
				head := make([]byte, len(capMagic))
				if _, readErr := io.ReadFull(conn, head); readErr != nil ||
					string(head) != capMagic {
					return
				}
				atomic.AddInt32(&server.accepts, 1)
				select {
				case server.started <- struct{}{}:
				default:
				}
				<-server.release
				_ = RespondProbe(conn, identity, instanceID, peerName,
					[]string{CapFolder})
			}(conn)
		}
	}()
	return server
}

func waitStarted(t *testing.T, server *gatedProbeServer, what string) {
	t.Helper()
	select {
	case <-server.started:
	case <-time.After(3 * time.Second):
		t.Fatalf("%s did not reach the probe server", what)
	}
}

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

// TestVerifyCandidateDiscardsResultForReplacedEndpoint reproduces the Wi-Fi
// handover rollback: a verification completes after the endpoint it departed
// from was replaced, and its stale Host/Port must be discarded.
func TestVerifyCandidateDiscardsResultForReplacedEndpoint(t *testing.T) {
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	server := startGatedValidProbeServer(t, identity, vecInstanceID, "endpoint")
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "svc"
	original := &Peer{InstanceID: vecInstanceID, Name: "old endpoint",
		Host: "127.0.0.1", Hosts: []string{"127.0.0.1"},
		Port: server.addr.Port, ServiceName: key}
	discovery.peers[key] = original

	discovery.verifyCandidate(key, "old endpoint", []string{"127.0.0.1"},
		server.addr.Port, vecInstanceID)
	waitStarted(t, server, "stale verification")
	replacement := &Peer{InstanceID: vecInstanceID, Name: "new endpoint",
		Host: "192.0.2.88", Hosts: []string{"192.0.2.88"}, Port: 41888,
		ServiceName: key}
	discovery.mu.Lock()
	discovery.peers[key] = replacement
	discovery.mu.Unlock()
	close(server.release)
	discovery.wg.Wait()
	if current := discovery.peers[key]; current != replacement {
		t.Fatalf("stale verification rolled the endpoint back: %#v", current)
	}
}

// TestVerifyCandidateMergeDiscardsResultForReplacedEndpoint is the same
// regression through the cross-source merge path.
func TestVerifyCandidateMergeDiscardsResultForReplacedEndpoint(t *testing.T) {
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	server := startGatedValidProbeServer(t, identity, vecInstanceID, "endpoint")
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	original := &Peer{InstanceID: vecInstanceID, Name: "old endpoint",
		Host: "192.0.2.60", Hosts: []string{"192.0.2.60"}, Port: 41600,
		ServiceName: "mdns-svc"}
	discovery.peers["mdns-svc"] = original

	discovery.verifyCandidate("broadcast|"+vecInstanceID, "",
		[]string{"127.0.0.1"}, server.addr.Port, vecInstanceID)
	waitStarted(t, server, "stale verification")
	replacement := &Peer{InstanceID: vecInstanceID, Name: "new endpoint",
		Host: "192.0.2.61", Hosts: []string{"192.0.2.61"}, Port: 41601,
		ServiceName: "mdns-svc"}
	discovery.mu.Lock()
	discovery.peers["mdns-svc"] = replacement
	discovery.mu.Unlock()
	close(server.release)
	discovery.wg.Wait()
	if current := discovery.peers["mdns-svc"]; current != replacement {
		t.Fatalf("stale merge rolled the endpoint back: %#v", current)
	}
	if len(discovery.peers) != 1 {
		t.Fatalf("stale merge left extra entries: %#v", discovery.peers)
	}
}

// TestSecondNICVerificationMergesAddresses pins the pure-broadcast multi-homed
// fix: the pass through the second NIC must add its address, not erase the
// first one.
func TestSecondNICVerificationMergesAddresses(t *testing.T) {
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, addr := startCloseableProbeServer(t, identity, vecInstanceID,
		"multi-homed", []string{CapFolder})
	defer listener.Close()
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "broadcast|" + vecInstanceID
	discovery.peers[key] = &Peer{InstanceID: vecInstanceID, Name: "multi-homed",
		Host: "192.0.2.70", Hosts: []string{"192.0.2.70"}, Port: addr.Port,
		ServiceName: key}

	discovery.verifyCandidate(key, "", []string{addr.IP.String()}, addr.Port,
		vecInstanceID)
	discovery.wg.Wait()
	peer := discovery.peers[key]
	if peer == nil || peer.Host != addr.IP.String() {
		t.Fatalf("second NIC pass did not take over as primary: %#v", peer)
	}
	if !stringInSlice("192.0.2.70", peer.Hosts) {
		t.Fatalf("second NIC pass erased the first NIC address: %v", peer.Hosts)
	}
}

// TestAnnouncementFromKnownSecondaryAddressIsNotNews pins the dedupe half of
// the multi-homed fix: an announcement from an address we already verified
// must not dispatch another verification (which would flip the primary).
func TestAnnouncementFromKnownSecondaryAddressIsNotNews(t *testing.T) {
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "broadcast|" + vecInstanceID
	discovery.peers[key] = &Peer{InstanceID: vecInstanceID, Name: "multi-homed",
		Host: "192.0.2.70", Hosts: []string{"192.0.2.70", "127.0.0.1"},
		Port: 41300, ServiceName: key}

	discovery.handleAnnouncement("127.0.0.1",
		&Announcement{InstanceID: vecInstanceID, Port: 41300})
	discovery.mu.Lock()
	pending := len(discovery.verinstr)
	discovery.mu.Unlock()
	if pending != 0 {
		t.Fatal("announcement from a known secondary address re-dispatched verification")
	}
	discovery.wg.Wait()
}

// TestGoodbyeCooldownCountsFromProbeCompletion pins the throttle semantics: a
// verification that outlives the cooldown must not let the next goodbye start
// another probe immediately.
func TestGoodbyeCooldownCountsFromProbeCompletion(t *testing.T) {
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	server := startGatedValidProbeServer(t, identity, vecInstanceID, "alive")
	discovery, cancel := snapshotTestDiscovery()
	defer cancel()
	key := "svc"
	discovery.peers[key] = &Peer{InstanceID: vecInstanceID, Name: "alive",
		Host: "127.0.0.1", Hosts: []string{"127.0.0.1"},
		Port: server.addr.Port, ServiceName: key}
	bye := &Announcement{InstanceID: vecInstanceID, Port: server.addr.Port,
		Bye: true}

	discovery.handleGoodbye("127.0.0.1", bye)
	waitStarted(t, server, "first goodbye verification")
	// Outlive the 500ms cooldown while the probe is still running.
	time.Sleep(600 * time.Millisecond)
	close(server.release)
	discovery.wg.Wait()

	discovery.handleGoodbye("127.0.0.1", bye)
	time.Sleep(150 * time.Millisecond)
	if got := atomic.LoadInt32(&server.accepts); got != 1 {
		t.Fatalf("goodbye right after a slow verification started %d probes, want 1",
			got)
	}
	discovery.wg.Wait()
}
