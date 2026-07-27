package core

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hashicorp/yamux"
	william "github.com/psanford/wormhole-william/wormhole"
	cryptossh "golang.org/x/crypto/ssh"
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

type fakeSSHSessionClient struct {
	dial        func(network, address string) (net.Conn, error)
	sendRequest func() error
	closed      chan struct{}
	closeOnce   sync.Once
	closes      atomic.Int32
}

func testSSHPrivateKey(t *testing.T) string {
	t.Helper()
	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	block, err := cryptossh.MarshalPrivateKey(private, "")
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(block))
}

type listenerBlockedUntilClientClose struct {
	client *fakeSSHSessionClient
	closed atomic.Bool
}

func (l *listenerBlockedUntilClientClose) Accept() (net.Conn, error) {
	return nil, io.ErrClosedPipe
}

func (l *listenerBlockedUntilClientClose) Close() error {
	<-l.client.closed
	l.closed.Store(true)
	return nil
}

func (l *listenerBlockedUntilClientClose) Addr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)}
}

func (f *fakeSSHSessionClient) Close() error {
	f.closes.Add(1)
	f.closeOnce.Do(func() {
		if f.closed != nil {
			close(f.closed)
		}
	})
	return nil
}

func (f *fakeSSHSessionClient) Dial(network, address string) (net.Conn, error) {
	if f.dial == nil {
		return nil, io.ErrClosedPipe
	}
	return f.dial(network, address)
}

func (f *fakeSSHSessionClient) Listen(_, _ string) (net.Listener, error) {
	return nil, io.ErrClosedPipe
}

func (f *fakeSSHSessionClient) SendRequest(_ string, _ bool, _ []byte) (bool, []byte, error) {
	if f.sendRequest == nil {
		return true, nil, nil
	}
	return true, nil, f.sendRequest()
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

func TestInstalledWormholeBridgeStaysOpenUntilSessionClose(t *testing.T) {
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
	if !current.installBridge(sender) {
		t.Fatal("live session rejected bridge")
	}
	conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
	if err != nil {
		t.Fatalf("installed bridge closed before explicit cancellation: %v", err)
	}
	_ = conn.Close()

	if err := current.Close(); err != nil {
		t.Fatal(err)
	}
	if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
		_ = conn.Close()
		t.Fatal("bridge listener remained open after session close")
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

func TestClosedSSHSessionRejectsNewPeerEndpoint(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	key, err := generateNoiseKey()
	if err != nil {
		t.Fatal(err)
	}
	session := &sshListenerSession{
		ctx: ctx, cancel: cancel,
		peers: make(map[string]SSHPeer), endpoints: make(map[string]*sshPeerEndpoint),
		stateChanged: make(chan struct{}, 1),
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	_, err = session.addPeer(SSHPeer{
		InstanceID:  "33333333333333333333333333333333",
		RemotePort:  32000,
		NoisePublic: encodeNoisePublic(key.Public),
		EndToEnd:    true,
	})
	if !errors.Is(err, net.ErrClosed) {
		t.Fatalf("closed SSH session accepted a peer: %v", err)
	}
	if len(session.endpoints) != 0 {
		t.Fatal("closed SSH session retained a peer endpoint")
	}
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

func TestSSHDataAuthorizationRequiresMatchingEncryptionMode(t *testing.T) {
	peerKey, err := generateNoiseKey()
	if err != nil {
		t.Fatal(err)
	}
	session := &sshListenerSession{peers: map[string]SSHPeer{
		"phone-id": {
			InstanceID:  "phone-id",
			NoisePublic: encodeNoisePublic(peerKey.Public),
			EndToEnd:    true,
		},
	}}

	if session.authorizeDataPeer(
		dataHello{InstanceID: "phone-id", Encrypted: false}, peerKey.Public) {
		t.Fatal("peer with a mismatched encryption mode was accepted")
	}
	if !session.authorizeDataPeer(
		dataHello{InstanceID: "phone-id", Encrypted: true}, peerKey.Public) {
		t.Fatal("peer with the expected identity and encryption mode was rejected")
	}
	otherKey, err := generateNoiseKey()
	if err != nil {
		t.Fatal(err)
	}
	if session.authorizeDataPeer(
		dataHello{InstanceID: "phone-id", Encrypted: false}, otherKey.Public) {
		t.Fatal("peer with the wrong Noise identity was accepted")
	}
}

func TestSSHListenRejectsMissingIdentityWithSavedPeers(t *testing.T) {
	service := NewService()
	defer service.Close()
	start, _ := json.Marshal(StartParams{
		LocalTarget: "127.0.0.1:1", LocalToken: "token",
		DeviceName: "desktop", InstanceID: "desktop-id",
	})
	if _, err := service.handle("start", start); err != nil {
		t.Fatal(err)
	}
	peerKey, err := generateNoiseKey()
	if err != nil {
		t.Fatal(err)
	}
	params, _ := json.Marshal(sshListenParams{
		Profile: SSHProfile{
			Host: "example.invalid", Port: 22, User: "user", PrivateKey: "invalid",
			HostKeySHA256: "SHA256:test",
		},
		Peers: []SSHPeer{{
			InstanceID: "phone-id", RemotePort: 23456,
			NoisePublic: encodeNoisePublic(peerKey.Public),
		}},
	})
	_, err = service.listenSSH(params)
	if err == nil || !strings.Contains(err.Error(), "saved SSH Noise identity is unavailable") {
		t.Fatalf("missing saved identity error = %v", err)
	}
}

func TestSSHListenKeepsSavedPortDuringInitialOutage(t *testing.T) {
	service := NewService()
	defer service.Close()
	start, _ := json.Marshal(StartParams{
		LocalTarget: "127.0.0.1:1", LocalToken: "token",
		DeviceName: "desktop", InstanceID: "desktop-id",
	})
	if _, err := service.handle("start", start); err != nil {
		t.Fatal(err)
	}
	reconnected, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reconnected.Close() })
	var attempts atomic.Int32
	service.sshConnect = func(
		context.Context, SSHProfile, int,
	) (sshSessionClient, net.Listener, int, error) {
		if attempts.Add(1) == 1 {
			return nil, nil, 0, errors.New("relay temporarily offline")
		}
		return &fakeSSHSessionClient{closed: make(chan struct{})},
			reconnected, 32577, nil
	}
	params, _ := json.Marshal(sshListenParams{
		Profile: SSHProfile{
			Host: "relay.example", Port: 22, User: "user", PrivateKey: testSSHPrivateKey(t),
			HostKeySHA256: "SHA256:test",
		},
		RemotePort: 32577,
	})
	result, err := service.listenSSH(params)
	if err != nil {
		t.Fatal(err)
	}
	values := result.(map[string]any)
	if values["remote_port"] != 32577 || values["connected"] != false {
		t.Fatalf("unexpected offline result: %+v", values)
	}
	if service.getSession(values["session_id"].(string)) == nil {
		t.Fatal("offline SSH session was not retained for reconnect")
	}
	select {
	case event := <-service.Events():
		if event.Event != "ssh.disconnected" {
			t.Fatalf("unexpected event: %+v", event)
		}
	case <-time.After(time.Second):
		t.Fatal("offline SSH session did not enter reconnect state")
	}
	select {
	case event := <-service.Events():
		if event.Event != "ssh.connected" {
			t.Fatalf("unexpected recovery event: %+v", event)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("offline SSH session did not reconnect")
	}
}

func TestSSHListenWithoutSavedPortStillRequiresServer(t *testing.T) {
	service := NewService()
	defer service.Close()
	start, _ := json.Marshal(StartParams{
		LocalTarget: "127.0.0.1:1", LocalToken: "token",
		DeviceName: "desktop", InstanceID: "desktop-id",
	})
	if _, err := service.handle("start", start); err != nil {
		t.Fatal(err)
	}
	service.sshConnect = func(
		context.Context, SSHProfile, int,
	) (sshSessionClient, net.Listener, int, error) {
		return nil, nil, 0, errors.New("relay temporarily offline")
	}
	params, _ := json.Marshal(sshListenParams{Profile: SSHProfile{
		Host: "relay.example", Port: 22, User: "user", PrivateKey: testSSHPrivateKey(t),
		HostKeySHA256: "SHA256:test",
	}})
	if _, err := service.listenSSH(params); err == nil ||
		!strings.Contains(err.Error(), "temporarily offline") {
		t.Fatalf("new relay startup error = %v", err)
	}
}

func TestSSHKeepaliveTimeoutInvalidatesBlackholedConnection(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	closed := make(chan struct{})
	client := &fakeSSHSessionClient{
		closed: closed,
		sendRequest: func() error {
			<-closed
			return io.ErrClosedPipe
		},
	}
	session := &sshListenerSession{
		ctx:          ctx,
		client:       client,
		stateChanged: make(chan struct{}, 1),
	}
	done := make(chan struct{})
	finished := make(chan struct{})
	go func() {
		session.keepaliveWithTiming(client, done, time.Millisecond, 10*time.Millisecond)
		close(finished)
	}()

	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("blackholed keepalive did not time out")
	}
	if client.closes.Load() != 1 {
		t.Fatalf("blackholed client close count = %d", client.closes.Load())
	}
	if session.waitForClient(nil, time.Millisecond) != nil {
		t.Fatal("timed-out SSH client remained available")
	}
}

func TestSSHTransferMuxDoesNotRunCompetingKeepalive(t *testing.T) {
	config := sshMuxConfig()
	if config.EnableKeepAlive {
		t.Fatal("per-transfer yamux keepalive can interrupt an active file stream")
	}
	if config.ConnectionWriteTimeout != sshMuxWriteTimeout {
		t.Fatalf("write timeout = %v", config.ConnectionWriteTimeout)
	}
}

type testMuxDialer struct {
	t        *testing.T
	mu       sync.Mutex
	dials    int
	servers  []*yamux.Session
	accepted chan net.Conn
}

func newTestMuxDialer(t *testing.T) *testMuxDialer {
	return &testMuxDialer{t: t, accepted: make(chan net.Conn, 8)}
}

func (d *testMuxDialer) dial(SSHPeer) (*yamux.Session, error) {
	left, right := net.Pipe()
	server, err := yamux.Server(right, sshMuxConfig())
	if err != nil {
		_ = left.Close()
		_ = right.Close()
		return nil, err
	}
	client, err := yamux.Client(left, sshMuxConfig())
	if err != nil {
		_ = server.Close()
		return nil, err
	}
	d.mu.Lock()
	d.dials++
	d.servers = append(d.servers, server)
	d.mu.Unlock()
	go func() {
		for {
			stream, acceptErr := server.Accept()
			if acceptErr != nil {
				return
			}
			d.accepted <- stream
		}
	}()
	return client, nil
}

func (d *testMuxDialer) count() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.dials
}

func (d *testMuxDialer) accept() net.Conn {
	d.t.Helper()
	select {
	case conn := <-d.accepted:
		return conn
	case <-time.After(time.Second):
		d.t.Fatal("yamux server did not accept stream")
		return nil
	}
}

func (d *testMuxDialer) close() {
	d.mu.Lock()
	servers := append([]*yamux.Session(nil), d.servers...)
	d.mu.Unlock()
	for _, server := range servers {
		_ = server.Close()
	}
}

func newTestMuxEndpoint(t *testing.T, dialer *testMuxDialer) *sshPeerEndpoint {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return &sshPeerEndpoint{
		listener: listener,
		closed:   make(chan struct{}),
		dialMux:  dialer.dial,
	}
}

func TestSSHPeerMuxReusesSequentialStreams(t *testing.T) {
	dialer := newTestMuxDialer(t)
	defer dialer.close()
	endpoint := newTestMuxEndpoint(t, dialer)
	defer endpoint.Close()

	first, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	firstRemote := dialer.accept()
	_ = first.Close()
	_ = firstRemote.Close()

	second, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	secondRemote := dialer.accept()
	defer second.Close()
	defer secondRemote.Close()
	if dialer.count() != 1 {
		t.Fatalf("sequential streams created %d mux sessions", dialer.count())
	}
}

func TestSSHPeerMuxExpiresOnlyAfterBecomingIdle(t *testing.T) {
	dialer := newTestMuxDialer(t)
	defer dialer.close()
	endpoint := newTestMuxEndpoint(t, dialer)
	defer endpoint.Close()

	first, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	firstRemote := dialer.accept()
	oldMux := endpoint.mux
	_ = first.Close()
	_ = firstRemote.Close()
	endpoint.muxMu.Lock()
	endpoint.muxLastIdle = time.Now().Add(-sshMuxReuseIdle - time.Second)
	endpoint.muxMu.Unlock()

	second, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	secondRemote := dialer.accept()
	defer second.Close()
	defer secondRemote.Close()
	if dialer.count() != 2 {
		t.Fatalf("expired idle session was reused; dial count = %d", dialer.count())
	}
	if !oldMux.IsClosed() {
		t.Fatal("expired idle mux remained open")
	}
}

func TestSSHPeerMuxNeverExpiresWithActiveStream(t *testing.T) {
	dialer := newTestMuxDialer(t)
	defer dialer.close()
	endpoint := newTestMuxEndpoint(t, dialer)
	defer endpoint.Close()

	first, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	firstRemote := dialer.accept()
	defer first.Close()
	defer firstRemote.Close()
	endpoint.muxMu.Lock()
	endpoint.muxLastIdle = time.Now().Add(-sshMuxReuseIdle - time.Second)
	endpoint.muxMu.Unlock()

	second, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	secondRemote := dialer.accept()
	defer second.Close()
	defer secondRemote.Close()
	if dialer.count() != 1 {
		t.Fatal("opening a second stream replaced a mux with an active stream")
	}

	_ = first.SetDeadline(time.Now().Add(time.Second))
	_ = firstRemote.SetDeadline(time.Now().Add(time.Second))
	if _, err := first.Write([]byte("still-open")); err != nil {
		t.Fatalf("active stream was closed: %v", err)
	}
	got := make([]byte, len("still-open"))
	if _, err := io.ReadFull(firstRemote, got); err != nil {
		t.Fatalf("active stream stopped carrying data: %v", err)
	}
}

func TestSSHPeerEndpointCloseReleasesCachedMux(t *testing.T) {
	dialer := newTestMuxDialer(t)
	defer dialer.close()
	endpoint := newTestMuxEndpoint(t, dialer)
	stream, err := endpoint.openRelayStream()
	if err != nil {
		t.Fatal(err)
	}
	remote := dialer.accept()
	mux := endpoint.mux
	_ = stream.Close()
	_ = remote.Close()

	if err := endpoint.Close(); err != nil {
		t.Fatal(err)
	}
	if !mux.IsClosed() || endpoint.mux != nil {
		t.Fatal("endpoint close did not release cached mux")
	}
}

func TestSSHInvalidationClosesClientBeforeReverseListener(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client := &fakeSSHSessionClient{closed: make(chan struct{})}
	listener := &listenerBlockedUntilClientClose{client: client}
	session := &sshListenerSession{
		ctx: ctx, client: client, reverse: listener,
		stateChanged: make(chan struct{}, 1),
	}
	done := make(chan struct{})
	go func() {
		session.invalidateClient(client)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("client invalidation deadlocked while closing the reverse listener")
	}
	if client.closes.Load() != 1 || !listener.closed.Load() {
		t.Fatal("SSH client and reverse listener were not both closed")
	}
}

func TestSSHDataDialWaitsForReplacementConnection(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	badClosed := make(chan struct{})
	bad := &fakeSSHSessionClient{
		closed: badClosed,
		dial: func(_, _ string) (net.Conn, error) {
			return nil, io.EOF
		},
	}
	remoteResult := make(chan net.Conn, 1)
	good := &fakeSSHSessionClient{dial: func(_, _ string) (net.Conn, error) {
		local, remote := net.Pipe()
		remoteResult <- remote
		return local, nil
	}}
	session := &sshListenerSession{
		ctx:          ctx,
		client:       bad,
		stateChanged: make(chan struct{}, 1),
	}
	go func() {
		<-badClosed
		session.setConnection(good, nil)
	}()

	conn, err := session.dialPeer(SSHPeer{RemotePort: 23456})
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	_ = (<-remoteResult).Close()
	if bad.closes.Load() != 1 {
		t.Fatalf("stale SSH client close count = %d", bad.closes.Load())
	}
}

func TestSSHPeerOfflineDoesNotInvalidateHealthyRelay(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client := &fakeSSHSessionClient{dial: func(_, _ string) (net.Conn, error) {
		return nil, &cryptossh.OpenChannelError{
			Reason:  cryptossh.ConnectionFailed,
			Message: "peer reverse port is offline",
		}
	}}
	session := &sshListenerSession{
		ctx:          ctx,
		client:       client,
		stateChanged: make(chan struct{}, 1),
	}

	_, err := session.dialPeerWithTiming(
		SSHPeer{RemotePort: 23456}, 20*time.Millisecond, time.Millisecond)
	var channelError *cryptossh.OpenChannelError
	if !errors.As(err, &channelError) {
		t.Fatalf("expected channel-open error, got %v", err)
	}
	if client.closes.Load() != 0 {
		t.Fatal("healthy SSH relay was closed because the peer was offline")
	}
	if session.waitForClient(nil, time.Millisecond) != client {
		t.Fatal("healthy SSH relay was removed")
	}
}

func TestSSHPeerDialWaitsForReversePortToReturn(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var attempts atomic.Int32
	remoteResult := make(chan net.Conn, 1)
	client := &fakeSSHSessionClient{dial: func(_, _ string) (net.Conn, error) {
		if attempts.Add(1) < 3 {
			return nil, &cryptossh.OpenChannelError{
				Reason: cryptossh.ConnectionFailed, Message: "peer reverse port is offline",
			}
		}
		local, remote := net.Pipe()
		remoteResult <- remote
		return local, nil
	}}
	session := &sshListenerSession{
		ctx: ctx, client: client, stateChanged: make(chan struct{}, 1),
	}

	conn, err := session.dialPeerWithTiming(
		SSHPeer{RemotePort: 23456}, time.Second, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	_ = (<-remoteResult).Close()
	if attempts.Load() != 3 {
		t.Fatalf("dial attempts = %d", attempts.Load())
	}
	if client.closes.Load() != 0 {
		t.Fatal("healthy SSH relay was invalidated while waiting for the peer")
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

func TestStreamBridgeClosesLoopbackEndpointWithSessionContext(t *testing.T) {
	left, right := net.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	receiver, err := newReceivingBridge(ctx, right, "127.0.0.1:1", "unused")
	if err != nil {
		t.Fatal(err)
	}
	sender, err := newSendingBridge(ctx, left)
	if err != nil {
		_ = receiver.Close()
		t.Fatal(err)
	}
	address := sender.Addr()
	cancel()

	deadline := time.Now().Add(time.Second)
	for {
		conn, dialErr := net.DialTimeout("tcp", address, 50*time.Millisecond)
		if dialErr != nil {
			break
		}
		_ = conn.Close()
		if time.Now().After(deadline) {
			t.Fatal("cancelled bridge listener remained open")
		}
		time.Sleep(10 * time.Millisecond)
	}
	_ = sender.Close()
	_ = receiver.Close()
}

func testNoisePublic(t *testing.T) string {
	t.Helper()
	key, err := generateNoiseKey()
	if err != nil {
		t.Fatal(err)
	}
	return encodeNoisePublic(key.Public)
}
