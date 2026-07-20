package core

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	william "github.com/psanford/wormhole-william/wormhole"
)

type blockingSession struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (b *blockingSession) Close() error {
	b.once.Do(func() { close(b.started) })
	<-b.release
	return nil
}

type countingSession struct{ closes atomic.Int32 }

func (c *countingSession) Close() error {
	c.closes.Add(1)
	return nil
}

func TestServiceProtocol(t *testing.T) {
	service := NewService()
	defer service.Close()

	var response Response
	if err := json.Unmarshal([]byte(service.HandleJSON(`{"id":"1","method":"ping"}`)), &response); err != nil {
		t.Fatal(err)
	}
	if !response.OK || response.ID != "1" {
		t.Fatalf("unexpected ping response: %+v", response)
	}

	bad := service.HandleJSON(`{"id":"2","method":"start","params":{}}`)
	if err := json.Unmarshal([]byte(bad), &response); err != nil {
		t.Fatal(err)
	}
	if response.OK || response.Error == "" {
		t.Fatalf("invalid start was accepted: %+v", response)
	}
}

func TestServiceDoesNotHoldMutexWhileClosingSessions(t *testing.T) {
	service := NewService()
	blocked := &blockingSession{started: make(chan struct{}), release: make(chan struct{})}
	service.putSession("blocked", blocked)
	removed := make(chan bool, 1)
	go func() { removed <- service.removeSession("blocked") }()
	select {
	case <-blocked.started:
	case <-time.After(time.Second):
		t.Fatal("session close did not start")
	}

	inserted := &countingSession{}
	putDone := make(chan struct{})
	go func() {
		service.putSession("other", inserted)
		close(putDone)
	}()
	select {
	case <-putDone:
	case <-time.After(time.Second):
		t.Fatal("unrelated service call blocked behind session Close")
	}
	close(blocked.release)
	if !<-removed {
		t.Fatal("session was not removed")
	}
	_ = service.Close()
	if inserted.closes.Load() != 1 {
		t.Fatalf("replacement session closed %d times", inserted.closes.Load())
	}
}

func TestWormholeSessionRejectsStateAfterCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	current := &wormholeSession{ctx: ctx, cancel: cancel}
	if current.currentOffer() != nil {
		t.Fatal("new session unexpectedly has an offer")
	}
	if err := current.Close(); err != nil {
		t.Fatal(err)
	}
	if current.setOffer(nil) {
		t.Fatal("cancelled session accepted an offer")
	}
	if current.currentOffer() != nil {
		t.Fatal("cancelled session exposed an offer")
	}
}

func TestWormholeBridgeCreatedAfterCancellationIsClosed(t *testing.T) {
	left, right := net.Pipe()
	bridgeContext, bridgeCancel := context.WithCancel(context.Background())
	defer bridgeCancel()
	receiver, err := newReceivingBridge(
		bridgeContext, right, "127.0.0.1:1", "unused")
	if err != nil {
		t.Fatal(err)
	}
	defer receiver.Close()
	sender, err := newSendingBridge(bridgeContext, left)
	if err != nil {
		t.Fatal(err)
	}
	address := sender.Addr()

	ctx, cancel := context.WithCancel(context.Background())
	current := &wormholeSession{ctx: ctx, cancel: cancel}
	if err := current.Close(); err != nil {
		t.Fatal(err)
	}
	if current.installBridge(sender) {
		t.Fatal("cancelled session installed a bridge")
	}
	if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
		_ = conn.Close()
		t.Fatal("rejected bridge listener remained open")
	}
}

func TestWormholeOfferAccessCanRaceWithCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	current := &wormholeSession{ctx: ctx, cancel: cancel}
	var group sync.WaitGroup
	for range 8 {
		group.Add(1)
		go func() {
			defer group.Done()
			for range 1000 {
				_ = current.setOffer(nil)
				_ = current.currentOffer()
			}
		}()
	}
	group.Add(1)
	go func() {
		defer group.Done()
		_ = current.Close()
	}()
	group.Wait()
	if current.currentOffer() != nil {
		t.Fatal("cancelled session exposed an offer")
	}
}

func TestWormholeOfferCanOnlyBeClaimedOnce(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	current := &wormholeSession{
		ctx: ctx, cancel: cancel, offer: new(william.TunnelOffer),
	}
	var claimed atomic.Int32
	var group sync.WaitGroup
	for range 16 {
		group.Add(1)
		go func() {
			defer group.Done()
			if current.claimOffer() != nil {
				claimed.Add(1)
			}
		}()
	}
	group.Wait()
	if claimed.Load() != 1 {
		t.Fatalf("offer was claimed %d times", claimed.Load())
	}
	current.mu.Lock()
	current.finished = true
	current.mu.Unlock()
	_ = current.Close()
}

func TestCapabilityToken(t *testing.T) {
	for _, valid := range []bool{true, false} {
		server, client := net.Pipe()
		got := make(chan bool, 1)
		go func() {
			got <- authenticateLoopback(server, "correct-token")
			_ = server.Close()
		}()
		token := "wrong-token"
		if valid {
			token = "correct-token"
		}
		_, _ = client.Write([]byte("IKAT" + token))
		_ = client.Close()
		if result := <-got; result != valid {
			t.Fatalf("valid=%v result=%v", valid, result)
		}
	}
}

func TestNoiseIKEncryptedAndAuthenticatedPlainModes(t *testing.T) {
	for _, encrypted := range []bool{true, false} {
		initiatorKey, err := generateNoiseKey()
		if err != nil {
			t.Fatal(err)
		}
		responderKey, err := generateNoiseKey()
		if err != nil {
			t.Fatal(err)
		}
		left, right := net.Pipe()
		responderResult := make(chan net.Conn, 1)
		responderErr := make(chan error, 1)
		go func() {
			raw, peer, payload, send, receive, err := noiseResponder(right, responderKey)
			if err != nil {
				responderErr <- err
				return
			}
			if string(peer) != string(initiatorKey.Public) || string(payload) != "hello" {
				responderErr <- io.ErrUnexpectedEOF
				return
			}
			responderResult <- finishNoiseResponder(raw, send, receive, encrypted)
		}()
		initiator, err := noiseInitiator(left, initiatorKey, responderKey.Public, []byte("hello"), encrypted)
		if err != nil {
			t.Fatal(err)
		}
		var responder net.Conn
		select {
		case err := <-responderErr:
			t.Fatal(err)
		case responder = <-responderResult:
		}
		go func() { _, _ = initiator.Write([]byte("payload")) }()
		got := make([]byte, len("payload"))
		if _, err := io.ReadFull(responder, got); err != nil {
			t.Fatal(err)
		}
		if string(got) != "payload" {
			t.Fatalf("unexpected payload %q", got)
		}
		_ = initiator.Close()
		_ = responder.Close()
	}
}

func TestSSHPairingPAKE(t *testing.T) {
	left, right := net.Pipe()
	initiator := sshIdentity{Name: "Mac", InstanceID: "mac-id", RemotePort: 24001,
		NoisePublic: testNoisePublic(t)}
	responder := sshIdentity{Name: "Android", InstanceID: "android-id", RemotePort: 24002,
		NoisePublic: testNoisePublic(t)}
	gotResponder := make(chan sshIdentity, 1)
	errCh := make(chan error, 1)
	go func() {
		peer, err := runPairResponder(right, "24002-alpha-beta", responder)
		if err != nil {
			errCh <- err
			return
		}
		if peer.InstanceID != initiator.InstanceID {
			errCh <- io.ErrUnexpectedEOF
			return
		}
		gotResponder <- peer
	}()
	peer, err := runPairInitiator(left, "24002-alpha-beta", initiator)
	if err != nil {
		t.Fatal(err)
	}
	if peer.InstanceID != responder.InstanceID {
		t.Fatalf("unexpected responder: %+v", peer)
	}
	select {
	case err := <-errCh:
		t.Fatal(err)
	case <-gotResponder:
	}
}

func TestStreamBridgeMultiplexesLoopbackConnections(t *testing.T) {
	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer echo.Close()
	go func() {
		for {
			conn, err := echo.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()

	left, right := net.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	receiver, err := newReceivingBridge(ctx, right, echo.Addr().String(), "inbound-token")
	if err != nil {
		t.Fatal(err)
	}
	defer receiver.Close()
	sender, err := newSendingBridge(ctx, left)
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()

	for _, value := range []string{"first", "second"} {
		conn, err := net.DialTimeout("tcp", sender.Addr(), time.Second)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = conn.Write([]byte("IKAT" + sender.Token() + value))
		got := make([]byte, len("IKCIinbound-token")+len(value))
		if _, err := io.ReadFull(conn, got); err != nil {
			t.Fatal(err)
		}
		if string(got) != "IKCIinbound-token"+value {
			t.Fatalf("got %q", got)
		}
		_ = conn.Close()
	}
}

func testNoisePublic(t *testing.T) string {
	t.Helper()
	key, err := generateNoiseKey()
	if err != nil {
		t.Fatal(err)
	}
	return encodeNoisePublic(key.Public)
}
