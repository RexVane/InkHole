package lan

import (
	"context"
	"io"
	"net"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// filterTestPeers returns only peers matching the expected instance IDs
func filterTestPeers(peers []Peer, expectedIDs ...string) []Peer {
	var filtered []Peer
	idSet := make(map[string]bool)
	for _, id := range expectedIDs {
		idSet[id] = true
	}
	for _, p := range peers {
		if idSet[p.InstanceID] {
			filtered = append(filtered, p)
		}
	}
	return filtered
}

// TestGoodbyeVerifiesBeforeRemoval confirms that a goodbye message triggers
// a probe before removing the peer, and only removes if the probe confirms
// the peer is truly gone.
func TestGoodbyeVerifiesBeforeRemoval(t *testing.T) {
	aliceID := randomInstanceID(t)
	bobID := randomInstanceID(t)

	aliceIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	bobIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}

	aliceListener, alicePort := startLANProbeServer(t, aliceIdentity, aliceID, "alice")
	defer aliceListener.Close()
	bobListener, bobPort := startLANProbeServer(t, bobIdentity, bobID, "bob")
	defer bobListener.Close()

	var bobPeers []Peer
	var mu sync.Mutex

	bobDisc, err := Start(Config{
		PeerName:     "bob",
		InstanceID:   bobID,
		Port:         bobPort,
		Identity:     bobIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func(peers []Peer) {
		mu.Lock()
		bobPeers = filterTestPeers(peers, aliceID)
		mu.Unlock()
	}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	defer bobDisc.Stop()

	aliceDisc, err := Start(Config{
		PeerName:     "alice",
		InstanceID:   aliceID,
		Port:         alicePort,
		Identity:     aliceIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func([]Peer) {}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if err != nil {
		t.Fatal(err)
	}

	// Wait for bob to discover alice
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		found := len(bobPeers) > 0
		mu.Unlock()
		if found {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	mu.Lock()
	if len(bobPeers) != 1 {
		t.Fatalf("bob should discover alice, got %d peers", len(bobPeers))
	}
	mu.Unlock()

	// Alice sends goodbye but stays online (malicious/buggy scenario)
	aliceDisc.broadcast.sayGoodbye()

	// Wait a bit - bob should probe and confirm alice is still alive
	time.Sleep(800 * time.Millisecond)

	mu.Lock()
	stillThere := len(bobPeers) == 1
	mu.Unlock()

	if !stillThere {
		t.Fatal("bob should keep alice in list since probe confirms she's still alive")
	}

	// Now alice actually stops (close listener first to ensure probe fails)
	aliceListener.Close()
	aliceDisc.Stop()

	// Bob should remove alice within a reasonable time
	deadline = time.Now().Add(2 * time.Second)
	removed := false
	for time.Now().Before(deadline) {
		mu.Lock()
		if len(bobPeers) == 0 {
			removed = true
			mu.Unlock()
			break
		}
		mu.Unlock()
		time.Sleep(50 * time.Millisecond)
	}

	if !removed {
		t.Fatal("bob should remove alice after goodbye + confirmed departure")
	}
}

// TestGoodbyeIgnoresUnknownPeer verifies that goodbye from an unknown
// instance ID is silently ignored.
func TestGoodbyeIgnoresUnknownPeer(t *testing.T) {
	aliceID := randomInstanceID(t)
	aliceIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}

	aliceListener, alicePort := startLANProbeServer(t, aliceIdentity, aliceID, "alice")
	defer aliceListener.Close()

	var alicePeers []Peer
	var mu sync.Mutex

	aliceDisc, err := Start(Config{
		PeerName:     "alice",
		InstanceID:   aliceID,
		Port:         alicePort,
		Identity:     aliceIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func(peers []Peer) {
		mu.Lock()
		alicePeers = filterTestPeers(peers, aliceID) // No peers expected
		mu.Unlock()
	}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	defer aliceDisc.Stop()

	// Inject a goodbye from an unknown peer directly into handleGoodbye
	// (simpler than trying to send UDP to the right port)
	fakeAnnouncement := &Announcement{
		InstanceID: "unknown-peer-id",
		Port:       9999,
		Bye:        true,
	}

	aliceDisc.handleGoodbye("127.0.0.1", fakeAnnouncement)

	// Wait and confirm alice's peer list unchanged (should be empty)
	time.Sleep(200 * time.Millisecond)

	mu.Lock()
	count := len(alicePeers)
	mu.Unlock()

	if count != 0 {
		t.Fatalf("alice should ignore goodbye from unknown peer, got %d peers", count)
	}
}

// TestGoodbyeThrottling verifies that rapid goodbye messages from the same
// peer are throttled to prevent DoS.
func TestGoodbyeThrottling(t *testing.T) {
	aliceID := randomInstanceID(t)
	bobID := randomInstanceID(t)

	aliceIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	bobIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}

	aliceListener, alicePort := startLANProbeServer(t, aliceIdentity, aliceID, "alice")
	defer aliceListener.Close()
	bobListener, bobPort := startLANProbeServer(t, bobIdentity, bobID, "bob")
	defer bobListener.Close()

	var bobPeers []Peer
	var mu sync.Mutex

	bobDisc, err := Start(Config{
		PeerName:     "bob",
		InstanceID:   bobID,
		Port:         bobPort,
		Identity:     bobIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func(peers []Peer) {
		mu.Lock()
		bobPeers = filterTestPeers(peers, aliceID)
		mu.Unlock()
	}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	defer bobDisc.Stop()

	aliceDisc, err := Start(Config{
		PeerName:     "alice",
		InstanceID:   aliceID,
		Port:         alicePort,
		Identity:     aliceIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func([]Peer) {}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	defer aliceDisc.Stop()

	// Manually inject alice as a discovered peer to bob
	bobDisc.handleEntry(mdnsEntry{
		ServiceName: "alice-service",
		InstanceID:  aliceID,
		PeerName:    "alice",
		Port:        alicePort,
		Hosts:       []string{"127.0.0.1"},
	})

	// Wait for verification to complete
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		found := len(bobPeers) > 0
		mu.Unlock()
		if found {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	mu.Lock()
	count := len(bobPeers)
	mu.Unlock()
	if count != 1 {
		t.Fatalf("discovery didn't complete in time (found %d peers, expected 1)", count)
	}

	// Track probe invocations by monitoring alice's connection attempts
	var probeCount int32
	aliceListener.Close()

	// Create a new listener that counts connection attempts
	newListener, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(alicePort))
	if err != nil {
		t.Fatal(err)
	}
	defer newListener.Close()

	go func() {
		for {
			conn, err := newListener.Accept()
			if err != nil {
				return
			}
			atomic.AddInt32(&probeCount, 1)
			go func(c net.Conn) {
				defer c.Close()
				_ = c.SetDeadline(time.Now().Add(5 * time.Second))
				head := make([]byte, 4)
				if _, err := io.ReadFull(c, head); err != nil || string(head) != capMagic {
					return
				}
				_ = RespondProbe(c, aliceIdentity, aliceID, "alice", []string{"folder-v1"})
			}(conn)
		}
	}()

	// Send rapid goodbye burst (simulating attack)
	// Only the first should trigger a probe goroutine, rest should be throttled
	for i := 0; i < 20; i++ {
		aliceDisc.broadcast.sayGoodbye()
		time.Sleep(10 * time.Millisecond)
	}

	// Wait for any spawned probes to complete
	time.Sleep(1 * time.Second)

	// Verify throttling: should see at most 2-3 probes (initial + maybe 1-2 more)
	// not 20 probes
	finalCount := atomic.LoadInt32(&probeCount)
	if finalCount > 5 {
		t.Errorf("throttling failed: expected ≤5 probes, got %d", finalCount)
	}

	// The system should still be responsive
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	done := make(chan bool)
	go func() {
		bobDisc.Refresh()
		done <- true
	}()

	select {
	case <-done:
		// Good - system is responsive
	case <-ctx.Done():
		t.Fatal("system became unresponsive after goodbye flood (throttling may have failed)")
	}
}

// TestGoodbyeAcceptsOnlyKnownHost verifies that goodbye from an IP not
// associated with the peer is rejected (anti-spoofing).
func TestGoodbyeAcceptsOnlyKnownHost(t *testing.T) {
	aliceID := randomInstanceID(t)
	bobID := randomInstanceID(t)

	aliceIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	bobIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}

	aliceListener, alicePort := startLANProbeServer(t, aliceIdentity, aliceID, "alice")
	defer aliceListener.Close()
	bobListener, bobPort := startLANProbeServer(t, bobIdentity, bobID, "bob")
	defer bobListener.Close()

	var bobPeers []Peer
	var mu sync.Mutex

	bobDisc, err := Start(Config{
		PeerName:     "bob",
		InstanceID:   bobID,
		Port:         bobPort,
		Identity:     bobIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func(peers []Peer) {
		mu.Lock()
		bobPeers = filterTestPeers(peers, aliceID)
		mu.Unlock()
	}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	defer bobDisc.Stop()

	aliceDisc, err := Start(Config{
		PeerName:     "alice",
		InstanceID:   aliceID,
		Port:         alicePort,
		Identity:     aliceIdentity,
		Capabilities: []string{"folder-v1"},
		LocalIPs:     []string{"127.0.0.1"},
	}, func([]Peer) {}, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	defer aliceDisc.Stop()

	// Manually inject alice as a discovered peer to bob
	bobDisc.handleEntry(mdnsEntry{
		ServiceName: "alice-service",
		InstanceID:  aliceID,
		PeerName:    "alice",
		Port:        alicePort,
		Hosts:       []string{"127.0.0.1"},
	})

	// Wait for verification to complete
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		found := len(bobPeers) > 0
		mu.Unlock()
		if found {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	mu.Lock()
	if len(bobPeers) != 1 {
		mu.Unlock()
		t.Fatalf("alice should be discovered, got %d peers", len(bobPeers))
	}
	aliceInstanceID := bobPeers[0].InstanceID
	mu.Unlock()

	// Send goodbye claiming to be alice but from a wrong host
	// Since alice is discovered from 127.0.0.1, sending from a different
	// IP should be rejected
	spoofedAnnouncement := &Announcement{
		InstanceID: aliceInstanceID,
		Port:       alicePort,
		Bye:        true,
	}

	// Call handleGoodbye with a host that's not in alice's known hosts
	bobDisc.handleGoodbye("192.168.99.99", spoofedAnnouncement)

	// Wait a bit - the goodbye should be ignored
	time.Sleep(300 * time.Millisecond)

	mu.Lock()
	stillPresent := len(bobPeers) == 1
	mu.Unlock()

	if !stillPresent {
		t.Fatal("bob should ignore goodbye from unknown host (spoofing attempt)")
	}
}
