package core

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/flynn/noise"
	"github.com/hashicorp/yamux"
	"golang.org/x/crypto/ssh"
)

const (
	sshModePair                 = "IKP1"
	sshModeData                 = "IKD1"
	sshKeepaliveInterval        = 30 * time.Second
	sshKeepaliveTimeout         = 30 * time.Second
	sshPeerReconnectWait        = 45 * time.Second
	sshPeerRetryInterval        = 500 * time.Millisecond
	sshMuxWriteTimeout          = 30 * time.Second
	sshMuxStreamWindow   uint32 = 4 * 1024 * 1024
)

type sshSessionClient interface {
	Close() error
	Dial(network, address string) (net.Conn, error)
	Listen(network, address string) (net.Listener, error)
	SendRequest(name string, wantReply bool, payload []byte) (bool, []byte, error)
}

type sshListenParams struct {
	Profile      SSHProfile `json:"profile"`
	RemotePort   int        `json:"remote_port,omitempty"`
	NoisePrivate string     `json:"noise_private,omitempty"`
	Peers        []SSHPeer  `json:"peers,omitempty"`
}

type sshIdentity struct {
	Name        string `json:"name"`
	InstanceID  string `json:"instance_id"`
	RemotePort  int    `json:"remote_port"`
	NoisePublic string `json:"noise_public"`
}

type dataHello struct {
	InstanceID string `json:"instance_id"`
	Encrypted  bool   `json:"encrypted"`
}

type sshListenerSession struct {
	service     *Service
	ctx         context.Context
	cancel      context.CancelFunc
	profile     SSHProfile
	key         noise.DHKey
	target      string
	targetToken string
	identity    sshIdentity

	mu           sync.RWMutex
	client       sshSessionClient
	reverse      net.Listener
	remotePort   int
	peers        map[string]SSHPeer
	endpoints    map[string]*sshPeerEndpoint
	pairCode     string
	pairExpiry   time.Time
	stateChanged chan struct{}
	wg           sync.WaitGroup
}

type sshPeerEndpoint struct {
	owner    *sshListenerSession
	peer     SSHPeer
	listener net.Listener
	closed   chan struct{}
	token    string
	wg       sync.WaitGroup
}

func (s *Service) listenSSH(raw json.RawMessage) (any, error) {
	var params sshListenParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	if err := normalizeSSHProfile(&params.Profile); err != nil {
		return nil, err
	}
	if params.Profile.HostKeySHA256 == "" {
		return nil, errors.New("confirm the SSH host fingerprint before enabling the relay")
	}
	target, targetToken, err := s.target()
	if err != nil {
		return nil, err
	}
	if params.NoisePrivate == "" && len(params.Peers) > 0 {
		return nil, errors.New(
			"saved SSH Noise identity is unavailable; remove paired devices and pair again")
	}
	var key noise.DHKey
	generated := false
	if params.NoisePrivate == "" {
		key, err = generateNoiseKey()
		generated = true
	} else {
		key, err = decodeNoiseKey(params.NoisePrivate)
	}
	if err != nil {
		return nil, err
	}
	client, reverse, remotePort, err := connectSSHReverse(s.ctx, params.Profile, params.RemotePort)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(s.ctx)
	current := &sshListenerSession{
		service:      s,
		ctx:          ctx,
		cancel:       cancel,
		profile:      params.Profile,
		key:          key,
		target:       target,
		targetToken:  targetToken,
		client:       client,
		reverse:      reverse,
		remotePort:   remotePort,
		peers:        make(map[string]SSHPeer),
		endpoints:    make(map[string]*sshPeerEndpoint),
		stateChanged: make(chan struct{}, 1),
	}
	current.identity = sshIdentity{
		Name:        s.deviceName,
		InstanceID:  s.instanceID,
		RemotePort:  remotePort,
		NoisePublic: encodeNoisePublic(key.Public),
	}
	for _, peer := range params.Peers {
		if _, err := current.addPeer(peer); err != nil {
			_ = current.Close()
			return nil, fmt.Errorf("invalid saved SSH peer: %w", err)
		}
	}
	id := randomID("ssh")
	s.putSession(id, current)
	current.wg.Add(1)
	go current.run()

	peers := current.peerList()
	result := map[string]any{
		"session_id":   id,
		"remote_port":  remotePort,
		"noise_public": current.identity.NoisePublic,
		"peers":        peers,
	}
	if generated {
		result["noise_private"] = encodeNoisePrivate(key)
	}
	return result, nil
}

func connectSSHReverse(ctx context.Context, profile SSHProfile, requestedPort int) (sshSessionClient, net.Listener, int, error) {
	if requestedPort < 0 || requestedPort > 65535 {
		return nil, nil, 0, errors.New("SSH remote port is invalid")
	}
	tries := 1
	if requestedPort == 0 {
		tries = 24
	}
	var lastErr error
	for attempt := 0; attempt < tries; attempt++ {
		port := requestedPort
		if port == 0 {
			port = randomRemotePort()
		}
		client, _, err := dialSSH(ctx, profile, false)
		if err != nil {
			return nil, nil, 0, err
		}
		listener, err := client.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
		if err == nil {
			return client, listener, port, nil
		}
		lastErr = err
		_ = client.Close()
		if requestedPort != 0 {
			break
		}
	}
	return nil, nil, 0, fmt.Errorf("cannot create SSH reverse forwarding: %w", lastErr)
}

func randomRemotePort() int {
	value, err := rand.Int(rand.Reader, big.NewInt(28000))
	if err != nil {
		return 30000 + int(time.Now().UnixNano()%10000)
	}
	return 20000 + int(value.Int64())
}

func (s *sshListenerSession) Close() error {
	s.cancel()
	s.mu.Lock()
	reverse := s.reverse
	client := s.client
	s.reverse = nil
	s.client = nil
	endpoints := make([]*sshPeerEndpoint, 0, len(s.endpoints))
	for _, endpoint := range s.endpoints {
		endpoints = append(endpoints, endpoint)
	}
	s.mu.Unlock()
	if client != nil {
		_ = client.Close()
	}
	if reverse != nil {
		_ = reverse.Close()
	}
	s.notifyStateChanged()
	for _, endpoint := range endpoints {
		_ = endpoint.Close()
	}
	s.wg.Wait()
	return nil
}

func (s *sshListenerSession) run() {
	defer s.wg.Done()
	backoff := time.Second
	for {
		s.mu.RLock()
		listener := s.reverse
		client := s.client
		s.mu.RUnlock()
		keepaliveDone := make(chan struct{})
		go s.keepalive(client, keepaliveDone)
		err := s.acceptLoop(listener)
		close(keepaliveDone)
		if s.ctx.Err() != nil {
			return
		}
		s.service.emit("ssh.disconnected", map[string]any{"error": errorString(err)})
		s.invalidateClient(client)

		for s.ctx.Err() == nil {
			select {
			case <-s.ctx.Done():
				return
			case <-time.After(backoff):
			}
			newClient, newListener, _, reconnectErr := connectSSHReverse(s.ctx, s.profile, s.remotePort)
			if reconnectErr != nil {
				if backoff < 30*time.Second {
					backoff *= 2
				}
				continue
			}
			s.setConnection(newClient, newListener)
			s.service.emit("ssh.connected", map[string]any{"remote_port": s.remotePort})
			backoff = time.Second
			break
		}
	}
}

func (s *sshListenerSession) notifyStateChanged() {
	select {
	case s.stateChanged <- struct{}{}:
	default:
	}
}

func (s *sshListenerSession) setConnection(client sshSessionClient, listener net.Listener) {
	s.mu.Lock()
	s.client = client
	s.reverse = listener
	s.mu.Unlock()
	s.notifyStateChanged()
}

func (s *sshListenerSession) invalidateClient(expected sshSessionClient) bool {
	if expected == nil {
		return false
	}
	s.mu.Lock()
	if s.client != expected {
		s.mu.Unlock()
		return false
	}
	reverse := s.reverse
	s.client = nil
	s.reverse = nil
	s.mu.Unlock()
	_ = expected.Close()
	if reverse != nil {
		_ = reverse.Close()
	}
	s.notifyStateChanged()
	return true
}

func (s *sshListenerSession) waitForClient(previous sshSessionClient, timeout time.Duration) sshSessionClient {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	for {
		s.mu.RLock()
		client := s.client
		s.mu.RUnlock()
		if client != nil && (previous == nil || client != previous) {
			return client
		}
		select {
		case <-s.ctx.Done():
			return nil
		case <-timer.C:
			return nil
		case <-s.stateChanged:
		}
	}
}

func (s *sshListenerSession) keepalive(client sshSessionClient, done <-chan struct{}) {
	s.keepaliveWithTiming(client, done, sshKeepaliveInterval, sshKeepaliveTimeout)
}

func (s *sshListenerSession) keepaliveWithTiming(client sshSessionClient, done <-chan struct{}, interval, timeout time.Duration) {
	if client == nil {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-done:
			return
		case <-s.ctx.Done():
			return
		case <-ticker.C:
			result := make(chan error, 1)
			go func() {
				_, _, err := client.SendRequest("keepalive@openssh.com", true, nil)
				result <- err
			}()
			timer := time.NewTimer(timeout)
			select {
			case <-done:
				timer.Stop()
				return
			case <-s.ctx.Done():
				timer.Stop()
				return
			case err := <-result:
				timer.Stop()
				if err == nil {
					continue
				}
				s.invalidateClient(client)
				return
			case <-timer.C:
				s.invalidateClient(client)
				return
			}
		}
	}
}

func (s *sshListenerSession) acceptLoop(listener net.Listener) error {
	if listener == nil {
		return errors.New("SSH forwarding listener is unavailable")
	}
	for {
		conn, err := listener.Accept()
		if err != nil {
			return err
		}
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.handleIncoming(conn)
		}()
	}
}

func (s *sshListenerSession) handleIncoming(conn net.Conn) {
	defer conn.Close()
	clearDeadline := setHandshakeDeadline(conn)
	mode := make([]byte, 4)
	if _, err := io.ReadFull(conn, mode); err != nil {
		return
	}
	switch string(mode) {
	case sshModePair:
		s.handlePair(conn)
	case sshModeData:
		secure, peerStatic, payload, send, receive, err := noiseResponder(conn, s.key)
		if err != nil {
			s.emitInboundDataError("noise", "", err)
			return
		}
		var hello dataHello
		if err := json.Unmarshal(payload, &hello); err != nil {
			s.emitInboundDataError("hello", "", err)
			return
		}
		if !s.authorizeDataPeer(hello, peerStatic) {
			s.emitInboundDataError("authorize", hello.InstanceID,
				errors.New("saved peer identity or encryption mode does not match"))
			return
		}
		clearDeadline()
		s.serveRemoteMux(finishNoiseResponder(secure, send, receive, hello.Encrypted))
	}
}

func (s *sshListenerSession) serveRemoteMux(conn net.Conn) {
	mux, err := yamux.Server(conn, sshMuxConfig())
	if err != nil {
		return
	}
	defer mux.Close()
	for {
		stream, err := mux.Accept()
		if err != nil {
			return
		}
		local, err := net.Dial("tcp", s.target)
		if err != nil {
			_ = stream.Close()
			continue
		}
		if _, err := local.Write([]byte("IKCI" + s.targetToken)); err != nil {
			_ = local.Close()
			_ = stream.Close()
			continue
		}
		go proxyConn(s.ctx, local, stream)
	}
}

func (s *sshListenerSession) authorizeDataPeer(hello dataHello, peerStatic []byte) bool {
	s.mu.RLock()
	peer, ok := s.peers[hello.InstanceID]
	s.mu.RUnlock()
	if !ok || peer.EndToEnd != hello.Encrypted {
		return false
	}
	expected, err := decodeNoisePublic(peer.NoisePublic)
	return err == nil && subtle.ConstantTimeCompare(expected, peerStatic) == 1
}

func (s *sshListenerSession) addPeer(peer SSHPeer) (SSHPeer, error) {
	peer.Name = stringsTrim(peer.Name)
	peer.InstanceID = stringsTrim(peer.InstanceID)
	if peer.ID == "" {
		peer.ID = peer.InstanceID
	}
	if peer.InstanceID == "" || peer.InstanceID == s.identity.InstanceID ||
		peer.RemotePort < 1 || peer.RemotePort > 65535 {
		return SSHPeer{}, errors.New("peer identity or remote port is invalid")
	}
	if _, err := decodeNoisePublic(peer.NoisePublic); err != nil {
		return SSHPeer{}, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return SSHPeer{}, err
	}
	endpoint := &sshPeerEndpoint{
		owner:    s,
		peer:     peer,
		listener: listener,
		closed:   make(chan struct{}),
		token:    newCapabilityToken(),
	}
	peer.Endpoint = listener.Addr().String()
	peer.EndpointToken = endpoint.token
	endpoint.peer = peer
	s.mu.Lock()
	old := s.endpoints[peer.InstanceID]
	s.peers[peer.InstanceID] = peer
	s.endpoints[peer.InstanceID] = endpoint
	s.mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
	endpoint.wg.Add(1)
	go endpoint.run()
	return peer, nil
}

func (s *sshListenerSession) peerList() []SSHPeer {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make([]SSHPeer, 0, len(s.peers))
	for _, peer := range s.peers {
		result = append(result, peer)
	}
	return result
}

func (e *sshPeerEndpoint) run() {
	defer e.wg.Done()
	for {
		local, err := e.listener.Accept()
		if err != nil {
			return
		}
		if !authenticateLoopback(local, e.token) {
			_ = local.Close()
			continue
		}
		e.wg.Add(1)
		go func() {
			defer e.wg.Done()
			remote, err := e.owner.openPeerStream(e.peer)
			if err != nil {
				e.owner.emitDataError(e.peer, "open", err)
				_ = local.Close()
				return
			}
			proxyConn(e.owner.ctx, local, remote)
		}()
	}
}

func (e *sshPeerEndpoint) Close() error {
	select {
	case <-e.closed:
		return nil
	default:
		close(e.closed)
	}
	err := e.listener.Close()
	e.wg.Wait()
	return err
}

func (s *sshListenerSession) openPeerStream(peer SSHPeer) (net.Conn, error) {
	raw, err := s.dialPeer(peer)
	if err != nil {
		return nil, err
	}
	fail := func(err error) (net.Conn, error) {
		_ = raw.Close()
		return nil, err
	}
	clearDeadline := setHandshakeDeadline(raw)
	if _, err := raw.Write([]byte(sshModeData)); err != nil {
		return fail(err)
	}
	peerPublic, err := decodeNoisePublic(peer.NoisePublic)
	if err != nil {
		return fail(err)
	}
	payload, _ := json.Marshal(dataHello{InstanceID: s.identity.InstanceID, Encrypted: peer.EndToEnd})
	secure, err := noiseInitiator(raw, s.key, peerPublic, payload, peer.EndToEnd)
	if err != nil {
		return fail(err)
	}
	clearDeadline()
	mux, err := yamux.Client(secure, sshMuxConfig())
	if err != nil {
		return fail(err)
	}
	stream, err := mux.Open()
	if err != nil {
		_ = mux.Close()
		return nil, err
	}
	return &muxStreamConn{Conn: stream, mux: mux}, nil
}

func (s *sshListenerSession) dialPeer(peer SSHPeer) (net.Conn, error) {
	return s.dialPeerWithTiming(peer, sshPeerReconnectWait, sshPeerRetryInterval)
}

func (s *sshListenerSession) dialPeerWithTiming(peer SSHPeer, timeout, retryInterval time.Duration) (net.Conn, error) {
	deadline := time.Now().Add(timeout)
	client := s.waitForClient(nil, timeout)
	if client == nil {
		return nil, errors.New("SSH relay reconnect timed out")
	}
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(peer.RemotePort))
	var lastErr error
	for time.Now().Before(deadline) {
		raw, err := client.Dial("tcp", address)
		if err == nil {
			return raw, nil
		}
		lastErr = err
		var channelError *ssh.OpenChannelError
		if errors.As(err, &channelError) {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				break
			}
			wait := retryInterval
			if wait > remaining {
				wait = remaining
			}
			select {
			case <-s.ctx.Done():
				return nil, s.ctx.Err()
			case <-time.After(wait):
			}
			continue
		}
		s.invalidateClient(client)
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		client = s.waitForClient(client, remaining)
		if client == nil {
			break
		}
	}
	if lastErr == nil {
		lastErr = errors.New("peer reverse port is offline")
	}
	return nil, fmt.Errorf("SSH relay data channel unavailable: %w", lastErr)
}

func (s *sshListenerSession) emitDataError(peer SSHPeer, stage string, err error) {
	s.service.emit("ssh.data.error", map[string]any{
		"peer_id":   peer.InstanceID,
		"peer_name": peer.Name,
		"stage":     stage,
		"error":     errorString(err),
	})
}

func (s *sshListenerSession) emitInboundDataError(stage, instanceID string, err error) {
	peer := SSHPeer{InstanceID: instanceID}
	s.mu.RLock()
	if saved, ok := s.peers[instanceID]; ok {
		peer = saved
	}
	s.mu.RUnlock()
	s.emitDataError(peer, stage, err)
}

func sshMuxConfig() *yamux.Config {
	config := yamux.DefaultConfig()
	config.KeepAliveInterval = sshKeepaliveInterval
	config.ConnectionWriteTimeout = sshMuxWriteTimeout
	config.MaxStreamWindowSize = sshMuxStreamWindow
	return config
}

type muxStreamConn struct {
	net.Conn
	mux *yamux.Session
}

func (c *muxStreamConn) Close() error {
	err := c.Conn.Close()
	_ = c.mux.Close()
	return err
}

func errorString(err error) string {
	if err == nil {
		return "connection closed"
	}
	return err.Error()
}

func stringsTrim(value string) string {
	return strings.TrimSpace(value)
}
