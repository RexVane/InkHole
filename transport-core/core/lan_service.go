package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/rexvane/inkhole/transport-core/core/lan"
)

// LAN service methods expose the shared discovery/transfer stack over the
// same JSON-RPC surface the desktop sidecar and the gomobile AAR already
// speak. Events: lan.peers, lan.status, lan.progress, lan.received,
// lan.sent.

type lanStartParams struct {
	PeerName        string   `json:"peer_name"`
	InstanceID      string   `json:"instance_id"`
	IdentityPrivate string   `json:"identity_private,omitempty"`
	Secret          string   `json:"secret,omitempty"`
	Inbox           string   `json:"inbox"`
	ListenPort      int      `json:"listen_port,omitempty"`
	Capabilities    []string `json:"capabilities,omitempty"`
	DisableMDNS     bool     `json:"disable_mdns,omitempty"`
	DisableUDP      bool     `json:"disable_udp,omitempty"`
}

type lanSendParams struct {
	SessionID     string `json:"session_id"`
	Path          string `json:"path"`
	InstanceID    string `json:"instance_id,omitempty"`
	Host          string `json:"host,omitempty"`
	Port          int    `json:"port,omitempty"`
	Fingerprint   string `json:"fingerprint,omitempty"`
	EndpointToken string `json:"endpoint_token,omitempty"`
}

type lanSession struct {
	service                *Service
	ctx                    context.Context
	cancel                 context.CancelFunc
	sessionID              string
	identity               *lan.Identity
	instanceID             string
	secret                 string
	outgoingStatePath      string
	advertisedName         string
	advertisedCapabilities []string
	ingressToken           string
	listener               net.Listener
	receiver               *lan.Receiver
	discovery              *lan.Discovery

	mu    sync.Mutex
	sends map[string]context.CancelFunc
	conns map[net.Conn]struct{}
	wg    sync.WaitGroup
}

func (s *Service) startLAN(raw json.RawMessage) (any, error) {
	var params lanStartParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	params.InstanceID = strings.ToLower(strings.TrimSpace(params.InstanceID))
	if params.PeerName == "" || params.Inbox == "" ||
		!lan.ValidInstanceID(params.InstanceID) {
		return nil, errors.New("peer_name, inbox and a valid instance_id are required")
	}
	identity, generated, err := loadOrCreateIdentity(params.IdentityPrivate)
	if err != nil {
		return nil, err
	}
	if len(params.Capabilities) == 0 {
		params.Capabilities = []string{lan.CapReliable, lan.CapFolder, lan.CapWHE4}
	}
	listener, err := net.Listen("tcp",
		net.JoinHostPort("", strconv.Itoa(params.ListenPort)))
	if err != nil {
		return nil, fmt.Errorf("cannot bind the transfer port: %w", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port

	ctx, cancel := context.WithCancel(s.ctx)
	id := randomID("lan")
	current := &lanSession{
		service:                s,
		ctx:                    ctx,
		cancel:                 cancel,
		sessionID:              id,
		identity:               identity,
		instanceID:             params.InstanceID,
		secret:                 params.Secret,
		outgoingStatePath:      filepath.Join(params.Inbox, ".inkhole-outgoing.json"),
		advertisedName:         params.PeerName,
		advertisedCapabilities: append([]string(nil), params.Capabilities...),
		ingressToken:           s.localIngressToken(),
		listener:               listener,
		sends:                  make(map[string]context.CancelFunc),
		conns:                  make(map[net.Conn]struct{}),
	}
	receiver, err := lan.NewReceiver(lan.ReceiverConfig{
		InboxDir:   params.Inbox,
		Secret:     params.Secret,
		Identity:   identity,
		InstanceID: params.InstanceID,
		OnProgress: func(filename string, done, total int64) {
			current.emit("lan.progress", map[string]any{
				"kind": "recv", "filename": filename,
				"done": done, "total": total,
			})
		},
		OnReceived: func(path string) {
			current.emit("lan.received", map[string]any{"path": path})
		},
		OnStatus: func(msg string) {
			current.emit("lan.status", map[string]any{"message": msg})
		},
	})
	if err != nil {
		cancel()
		_ = listener.Close()
		return nil, err
	}
	current.receiver = receiver

	discovery, err := lan.Start(lan.Config{
		PeerName:         params.PeerName,
		InstanceID:       params.InstanceID,
		Port:             port,
		Identity:         identity,
		Capabilities:     params.Capabilities,
		LocalIPs:         lan.LocalIPv4s(),
		DisableMDNS:      params.DisableMDNS,
		DisableBroadcast: params.DisableUDP,
	}, func(peers []lan.Peer) {
		current.emit("lan.peers", map[string]any{"peers": lanPeerList(peers)})
	}, func(msg string) {
		current.emit("lan.status", map[string]any{"message": msg})
	})
	if err != nil {
		cancel()
		_ = listener.Close()
		return nil, err
	}
	current.discovery = discovery

	current.wg.Add(1)
	go current.acceptLoop()
	s.putSession(id, current)

	result := map[string]any{
		"session_id":  id,
		"port":        port,
		"instance_id": params.InstanceID,
		"fingerprint": identity.Fingerprint,
		"public_key":  identity.PublicKey,
	}
	if generated {
		exported, err := identity.ExportPrivateKey()
		if err == nil {
			result["identity_private"] = exported
		}
	}
	return result, nil
}

func loadOrCreateIdentity(encoded string) (*lan.Identity, bool, error) {
	if strings.TrimSpace(encoded) != "" {
		identity, err := lan.IdentityFromPrivateKey(encoded)
		if err != nil {
			return nil, false, err
		}
		return identity, false, nil
	}
	identity, err := lan.GenerateIdentity()
	if err != nil {
		return nil, false, err
	}
	return identity, true, nil
}

func lanPeerList(peers []lan.Peer) []map[string]any {
	out := make([]map[string]any, 0, len(peers))
	for _, peer := range peers {
		out = append(out, map[string]any{
			"instance_id":  peer.InstanceID,
			"name":         peer.Name,
			"host":         peer.Host,
			"hosts":        peer.Hosts,
			"port":         peer.Port,
			"capabilities": peer.Capabilities,
			"fingerprint":  peer.Fingerprint,
			"public_key":   peer.PublicKey,
			"service_name": peer.ServiceName,
		})
	}
	return out
}

func (l *lanSession) emit(name string, data map[string]any) {
	if l.ctx.Err() != nil {
		return
	}
	data["session_id"] = l.sessionID
	l.service.emit(name, data)
}

// acceptLoop tracks inbound connections and hands each one to the shared
// lan.HandleInbound dispatcher; tracking lets Close unblock stalled
// handshakes so wg.Wait cannot hang.
func (l *lanSession) acceptLoop() {
	defer l.wg.Done()
	for {
		conn, err := l.listener.Accept()
		if err != nil {
			return
		}
		if !l.trackConn(conn) {
			_ = conn.Close()
			return
		}
		l.wg.Add(1)
		go func(conn net.Conn) {
			defer l.wg.Done()
			defer l.untrackConn(conn)
			lan.HandleInbound(conn, lan.InboundConfig{
				IngressToken: l.ingressToken,
				Identity:     l.identity,
				InstanceID:   l.instanceID,
				PeerName:     l.peerName,
				Capabilities: l.capabilities,
				Receiver:     l.receiver,
			})
		}(conn)
	}
}

// trackConn registers a live inbound connection; false after Close began.
func (l *lanSession) trackConn(conn net.Conn) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.conns == nil {
		return false
	}
	l.conns[conn] = struct{}{}
	return true
}

func (l *lanSession) untrackConn(conn net.Conn) {
	l.mu.Lock()
	if l.conns != nil {
		delete(l.conns, conn)
	}
	l.mu.Unlock()
}

func (l *lanSession) peerName() string {
	if l.advertisedName != "" {
		return l.advertisedName
	}
	return "InkHole"
}

func (l *lanSession) capabilities() []string {
	return append([]string(nil), l.advertisedCapabilities...)
}

func (s *Service) localIngressToken() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.localToken
}

func (l *lanSession) Close() error {
	l.cancel()
	if l.discovery != nil {
		l.discovery.Stop()
	}
	_ = l.listener.Close()
	l.mu.Lock()
	for _, cancel := range l.sends {
		cancel()
	}
	// Closing live connections unblocks any handler mid-read so wg.Wait
	// below cannot hang on a stalled peer.
	for conn := range l.conns {
		_ = conn.Close()
	}
	l.conns = nil
	l.mu.Unlock()
	l.wg.Wait()
	return nil
}

func (s *Service) lanSessionByID(raw json.RawMessage) (*lanSession, json.RawMessage, error) {
	var params struct {
		SessionID string `json:"session_id"`
	}
	if err := decodeParams(raw, &params); err != nil {
		return nil, raw, err
	}
	current := s.getSession(params.SessionID)
	lanCurrent, ok := current.(*lanSession)
	if !ok || lanCurrent == nil {
		return nil, raw, errors.New("unknown lan session")
	}
	return lanCurrent, raw, nil
}

func (s *Service) lanPeers(raw json.RawMessage) (any, error) {
	current, _, err := s.lanSessionByID(raw)
	if err != nil {
		return nil, err
	}
	return map[string]any{"peers": lanPeerList(current.discovery.Peers())}, nil
}

func (s *Service) lanSend(raw json.RawMessage) (any, error) {
	current, _, err := s.lanSessionByID(raw)
	if err != nil {
		return nil, err
	}
	var params lanSendParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	if params.Path == "" {
		return nil, errors.New("path is required")
	}
	info, err := os.Stat(params.Path)
	if err != nil {
		return nil, err
	}
	target, err := current.resolveTarget(params)
	if err != nil {
		return nil, err
	}
	sendID := randomID("send")
	ctx, cancel := context.WithCancel(current.ctx)
	current.mu.Lock()
	current.sends[sendID] = cancel
	current.mu.Unlock()
	current.wg.Add(1)
	go func() {
		defer current.wg.Done()
		defer func() {
			current.mu.Lock()
			delete(current.sends, sendID)
			current.mu.Unlock()
			cancel()
		}()
		cfg := lan.SenderConfig{
			Secret:            current.secret,
			Identity:          current.identity,
			InstanceID:        current.instanceID,
			OutgoingStatePath: current.outgoingStatePath,
			OnProgress: func(filename string, done, total int64) {
				current.emit("lan.progress", map[string]any{
					"kind": "send", "filename": filename,
					"done": done, "total": total, "send_id": sendID,
				})
			},
			OnStatus: func(msg string) {
				current.emit("lan.status", map[string]any{"message": msg})
			},
		}
		var sendErr error
		if info.IsDir() {
			sendErr = lan.SendFolder(ctx, target, params.Path, cfg)
		} else {
			sendErr = lan.SendFile(ctx, target, params.Path, cfg)
		}
		payload := map[string]any{
			"send_id": sendID, "path": params.Path, "ok": sendErr == nil,
		}
		if sendErr != nil {
			payload["error"] = errorString(sendErr)
		}
		current.emit("lan.sent", payload)
	}()
	return map[string]any{"send_id": sendID}, nil
}

func (current *lanSession) resolveTarget(params lanSendParams) (lan.SendTarget, error) {
	params.InstanceID = strings.ToLower(strings.TrimSpace(params.InstanceID))
	if params.InstanceID != "" {
		for _, peer := range current.discovery.Peers() {
			if peer.InstanceID == params.InstanceID {
				return lan.SendTarget{
					Host:        peer.Host,
					Hosts:       peer.Hosts,
					Port:        peer.Port,
					InstanceID:  peer.InstanceID,
					Fingerprint: peer.Fingerprint,
					UseWHE4:     lan.SupportsWHE4(peer.Capabilities),
				}, nil
			}
		}
	}
	if params.Host != "" && params.Port > 0 {
		// Manual endpoints carry no verified capability list, so encrypted
		// sends stay on WHE3, which every v3 peer accepts.
		return lan.SendTarget{
			Host:          params.Host,
			Port:          params.Port,
			InstanceID:    params.InstanceID,
			Fingerprint:   params.Fingerprint,
			EndpointToken: params.EndpointToken,
		}, nil
	}
	return lan.SendTarget{}, errors.New("target device is not available")
}

func (s *Service) lanSendCancel(raw json.RawMessage) (any, error) {
	current, _, err := s.lanSessionByID(raw)
	if err != nil {
		return nil, err
	}
	var params struct {
		SessionID string `json:"session_id"`
		SendID    string `json:"send_id"`
	}
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	current.mu.Lock()
	cancel := current.sends[params.SendID]
	current.mu.Unlock()
	if cancel == nil {
		return map[string]any{"cancelled": false}, nil
	}
	cancel()
	return map[string]any{"cancelled": true}, nil
}
