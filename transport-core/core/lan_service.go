package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

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
	SessionID   string `json:"session_id"`
	Path        string `json:"path"`
	InstanceID  string `json:"instance_id,omitempty"`
	Host        string `json:"host,omitempty"`
	Port        int    `json:"port,omitempty"`
	Fingerprint string `json:"fingerprint,omitempty"`
}

type lanSession struct {
	service    *Service
	ctx        context.Context
	cancel     context.CancelFunc
	sessionID  string
	identity   *lan.Identity
	instanceID string
	secret     string
	listener   net.Listener
	receiver   *lan.Receiver
	discovery  *lan.Discovery

	mu    sync.Mutex
	sends map[string]context.CancelFunc
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
		params.Capabilities = []string{lan.CapReliable, lan.CapFolder}
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
		service:    s,
		ctx:        ctx,
		cancel:     cancel,
		sessionID:  id,
		identity:   identity,
		instanceID: params.InstanceID,
		secret:     params.Secret,
		listener:   listener,
		sends:      make(map[string]context.CancelFunc),
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

// acceptLoop dispatches inbound connections by magic: WHPC probes answer
// inline, WHPP transfers hand off to the receiver.
func (l *lanSession) acceptLoop() {
	defer l.wg.Done()
	for {
		conn, err := l.listener.Accept()
		if err != nil {
			return
		}
		l.wg.Add(1)
		go func(conn net.Conn) {
			defer l.wg.Done()
			_ = conn.SetDeadline(time.Now().Add(15 * time.Second))
			head := make([]byte, 4)
			if _, err := io.ReadFull(conn, head); err != nil {
				_ = conn.Close()
				return
			}
			_ = conn.SetDeadline(time.Time{})
			switch string(head) {
			case "WHPC":
				_ = lan.RespondProbe(conn, l.identity, l.instanceID,
					l.peerName(), l.capabilities())
				_ = conn.Close()
			case "WHPP":
				l.receiver.HandleWHPP(conn)
			default:
				_ = conn.Close()
			}
		}(conn)
	}
}

func (l *lanSession) peerName() string {
	l.service.mu.RLock()
	defer l.service.mu.RUnlock()
	if l.service.deviceName != "" {
		return l.service.deviceName
	}
	return "InkHole"
}

func (l *lanSession) capabilities() []string {
	return []string{lan.CapReliable, lan.CapFolder}
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
			Secret:     current.secret,
			Identity:   current.identity,
			InstanceID: current.instanceID,
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
				}, nil
			}
		}
	}
	if params.Host != "" && params.Port > 0 {
		return lan.SendTarget{
			Host:        params.Host,
			Port:        params.Port,
			InstanceID:  params.InstanceID,
			Fingerprint: params.Fingerprint,
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
