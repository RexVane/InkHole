package core

import (
	"context"
	"io"
	"net"
	"testing"
	"time"
)

func testUDPSocket(t *testing.T) *net.UDPConn {
	t.Helper()
	sock, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("listen udp: %v", err)
	}
	return sock
}

func TestGenerateDirectCertFingerprint(t *testing.T) {
	cert, fp, err := generateDirectCert()
	if err != nil {
		t.Fatalf("generateDirectCert: %v", err)
	}
	if len(fp) != 64 {
		t.Fatalf("fingerprint length = %d, want 64 hex chars", len(fp))
	}
	if got := directCertFingerprint(cert.Certificate[0]); got != fp {
		t.Fatalf("fingerprint mismatch: %s != %s", got, fp)
	}
	_, fp2, err := generateDirectCert()
	if err != nil {
		t.Fatalf("second generateDirectCert: %v", err)
	}
	if fp2 == fp {
		t.Fatal("two generated certs share a fingerprint")
	}
}

func TestDirectResolveCandidatesFilters(t *testing.T) {
	got := directResolveCandidates([]string{
		"127.0.0.1:1234",
		"0.0.0.0:1234",
		"192.168.1.9:0",
		"not-an-address",
		"192.168.1.9:41300",
		"192.168.1.9:41300",
	})
	if len(got) != 1 || got[0].String() != "192.168.1.9:41300" {
		t.Fatalf("directResolveCandidates = %v, want [192.168.1.9:41300]", got)
	}
}

func TestDirectEstablishLoopback(t *testing.T) {
	sockA := testUDPSocket(t)
	sockB := testUDPSocket(t)
	certA, fpA, err := generateDirectCert()
	if err != nil {
		t.Fatalf("certA: %v", err)
	}
	certB, fpB, err := generateDirectCert()
	if err != nil {
		t.Fatalf("certB: %v", err)
	}
	sessionA := &sshListenerSession{ctx: context.Background()}
	sessionB := &sshListenerSession{ctx: context.Background()}

	type result struct {
		link *quicLink
		err  error
	}
	serverDone := make(chan result, 1)
	go func() {
		link, err := sessionB.directEstablish(
			sockB, false, fpA, certB,
			[]*net.UDPAddr{sockA.LocalAddr().(*net.UDPAddr)})
		serverDone <- result{link, err}
	}()
	linkA, err := sessionA.directEstablish(
		sockA, true, fpB, certA,
		[]*net.UDPAddr{sockB.LocalAddr().(*net.UDPAddr)})
	if err != nil {
		t.Fatalf("client establish: %v", err)
	}
	defer linkA.close("test done")
	server := <-serverDone
	if server.err != nil {
		t.Fatalf("server establish: %v", server.err)
	}
	defer server.link.close("test done")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := linkA.conn.OpenStreamSync(ctx)
	if err != nil {
		t.Fatalf("open stream: %v", err)
	}
	payload := []byte("hello-direct")
	if _, err := stream.Write(payload); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := stream.Close(); err != nil {
		t.Fatalf("close stream: %v", err)
	}
	accepted, err := server.link.conn.AcceptStream(ctx)
	if err != nil {
		t.Fatalf("accept stream: %v", err)
	}
	got, err := io.ReadAll(accepted)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(got) != string(payload) {
		t.Fatalf("payload = %q, want %q", got, payload)
	}
}

func TestDirectEstablishRejectsWrongFingerprint(t *testing.T) {
	sockA := testUDPSocket(t)
	sockB := testUDPSocket(t)
	certA, fpA, err := generateDirectCert()
	if err != nil {
		t.Fatalf("certA: %v", err)
	}
	certB, _, err := generateDirectCert()
	if err != nil {
		t.Fatalf("certB: %v", err)
	}
	sessionA := &sshListenerSession{ctx: context.Background()}
	sessionB := &sshListenerSession{ctx: context.Background()}
	serverDone := make(chan error, 1)
	go func() {
		link, err := sessionB.directEstablish(
			sockB, false, fpA, certB,
			[]*net.UDPAddr{sockA.LocalAddr().(*net.UDPAddr)})
		if link != nil {
			link.close("unexpected")
		}
		serverDone <- err
	}()
	// Client expects fpA but the server presents certB: the TLS verify
	// callback must refuse the handshake.
	link, err := sessionA.directEstablish(
		sockA, true, fpA, certA,
		[]*net.UDPAddr{sockB.LocalAddr().(*net.UDPAddr)})
	if err == nil {
		link.close("unexpected")
		t.Fatal("client establish succeeded with wrong fingerprint")
	}
	// Unblock the server accept instead of waiting out its timeout.
	_ = sockB.Close()
	<-serverDone
}

func TestDirectAttemptBelongsToSessionLifecycle(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	started := make(chan struct{})
	finished := make(chan struct{})
	session := &sshListenerSession{
		ctx:          ctx,
		cancel:       cancel,
		peers:        make(map[string]SSHPeer),
		endpoints:    make(map[string]*sshPeerEndpoint),
		stateChanged: make(chan struct{}, 1),
		directConnect: func(SSHPeer) error {
			close(started)
			<-ctx.Done()
			close(finished)
			return ctx.Err()
		},
	}
	session.maybeDirect(SSHPeer{InstanceID: "peer"})
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("direct attempt did not start")
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-finished:
	default:
		t.Fatal("session close returned before direct attempt exited")
	}
	session.directMu.Lock()
	defer session.directMu.Unlock()
	if session.directPending["peer"] {
		t.Fatal("completed direct attempt remained pending")
	}
}

func TestCancelledSessionDoesNotStartDirectAttempt(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	called := false
	session := &sshListenerSession{
		ctx: ctx,
		directConnect: func(SSHPeer) error {
			called = true
			return nil
		},
	}
	session.maybeDirect(SSHPeer{InstanceID: "peer"})
	if called {
		t.Fatal("cancelled session started a direct attempt")
	}
}

func TestDirectPublicEndpointStopsWhenContextIsCancelled(t *testing.T) {
	server := testUDPSocket(t)
	defer server.Close()
	oldServers := directSTUNServers
	directSTUNServers = []string{server.LocalAddr().String()}
	defer func() { directSTUNServers = oldServers }()

	sock := testUDPSocket(t)
	defer sock.Close()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if endpoint, ok := directPublicEndpoint(ctx, sock); ok || endpoint != "" {
			t.Errorf("cancelled STUN lookup returned %q, %v", endpoint, ok)
		}
	}()

	buf := make([]byte, 1500)
	_ = server.SetReadDeadline(time.Now().Add(time.Second))
	if _, _, err := server.ReadFromUDP(buf); err != nil {
		t.Fatalf("STUN server did not receive request: %v", err)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancelled STUN lookup did not stop")
	}
}
