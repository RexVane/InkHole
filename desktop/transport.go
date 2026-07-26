package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	transportcore "github.com/rexvane/inkhole/transport-core/core"
	"github.com/rexvane/inkhole/transport-core/core/lan"
)

var transportRequestID atomic.Uint64

type transportResponse struct {
	OK     bool            `json:"ok"`
	Result json.RawMessage `json:"result"`
	Error  string          `json:"error"`
}

type SSHSettingsInput struct {
	Enabled         bool   `json:"enabled"`
	Host            string `json:"host"`
	Port            int    `json:"port"`
	User            string `json:"user"`
	PrivateKeyMode  string `json:"privateKeyMode"`
	PrivateKeyPath  string `json:"privateKeyPath"`
	PastedKey       string `json:"pastedKey"`
	Passphrase      string `json:"passphrase"`
	ClearPassphrase bool   `json:"clearPassphrase"`
	HostKeySHA256   string `json:"hostKeySHA256"`
}

func callTransport(service *transportcore.Service, method string, params any) (map[string]any, error) {
	if service == nil {
		return nil, errors.New("跨网核心未运行")
	}
	request := map[string]any{
		"id":     fmt.Sprintf("desktop-%d", transportRequestID.Add(1)),
		"method": method,
	}
	if params != nil {
		request["params"] = params
	}
	raw, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	var response transportResponse
	if err := json.Unmarshal([]byte(service.HandleJSON(string(raw))), &response); err != nil {
		return nil, fmt.Errorf("跨网核心返回无效响应: %w", err)
	}
	if !response.OK {
		if response.Error == "" {
			response.Error = "跨网操作失败"
		}
		return nil, errors.New(response.Error)
	}
	if len(response.Result) == 0 || string(response.Result) == "null" {
		return map[string]any{}, nil
	}
	var result map[string]any
	if err := json.Unmarshal(response.Result, &result); err != nil {
		return nil, fmt.Errorf("跨网核心结果无效: %w", err)
	}
	return result, nil
}

func (s *InkHoleService) transportEventLoop(ctx context.Context, service *transportcore.Service) {
	defer s.nodeWG.Done()
	for {
		select {
		case <-ctx.Done():
			return
		case event := <-service.Events():
			s.handleTransportEvent(service, event)
		}
	}
}

func (s *InkHoleService) handleTransportEvent(service *transportcore.Service, event transportcore.Event) {
	data := eventMap(event.Data)
	switch event.Event {
	case "wormhole.ready":
		if stringValue(data["role"]) == "sender" {
			sessionID := stringValue(data["session_id"])
			s.mu.Lock()
			paths := append([]string(nil), s.pendingWormhole[sessionID]...)
			delete(s.pendingWormhole, sessionID)
			s.mu.Unlock()
			endpoint := stringValue(data["local_endpoint"])
			token := stringValue(data["endpoint_token"])
			host, port := splitEndpoint(endpoint)
			if len(paths) == 0 || host == "" || port == 0 || token == "" {
				s.emit("transport", map[string]any{"event": "wormhole.error",
					"data": map[string]any{"session_id": sessionID,
						"error": "一次性传输端点无效"}})
				break
			}
			if _, err := s.startEndpointSend(paths, lan.SendTarget{
				Host: host, Port: port, EndpointToken: token,
			}, func() {
				_, _ = callTransport(service, "session.cancel", map[string]any{"session_id": sessionID})
			}); err != nil {
				s.emit("transport", map[string]any{"event": "wormhole.error",
					"data": map[string]any{"session_id": sessionID, "error": err.Error()}})
			}
		}
	case "wormhole.error":
		s.mu.Lock()
		delete(s.pendingWormhole, stringValue(data["session_id"]))
		s.mu.Unlock()
	case "ssh.paired":
		if peer := runtimePeer(data["peer"]); peer.InstanceID != "" {
			s.rememberSSHPeer(peer)
		}
	case "ssh.connected":
		s.mu.Lock()
		s.sshConnected = true
		s.mu.Unlock()
		s.emit("status", "SSH 中继已连接")
	case "ssh.disconnected":
		s.mu.Lock()
		s.sshConnected = false
		s.mu.Unlock()
		s.emit("status", "SSH 中继暂时不可用，正在自动重连")
	case "ssh.direct.up":
		s.emit("status", "已升级为 QUIC 点对点直连")
	case "ssh.direct.down":
		s.emit("status", "QUIC 直连已断开，继续使用 SSH 中继")
	case "ssh.data.error":
		message := stringValue(data["error"])
		if message != "" {
			s.emit("status", "SSH 传输通道异常："+message)
		}
	}
	s.emit("transport", map[string]any{"event": event.Event, "data": data})
}

func eventMap(value any) map[string]any {
	if result, ok := value.(map[string]any); ok {
		return result
	}
	raw, _ := json.Marshal(value)
	var result map[string]any
	_ = json.Unmarshal(raw, &result)
	if result == nil {
		result = make(map[string]any)
	}
	return result
}

func stringValue(value any) string {
	result, _ := value.(string)
	return result
}

func runtimePeer(value any) runtimeSSHPeer {
	raw, _ := json.Marshal(value)
	var peer runtimeSSHPeer
	_ = json.Unmarshal(raw, &peer)
	return peer
}

func (s *InkHoleService) CreateOneTime(paths []string) (map[string]any, error) {
	normalized, err := normalizeSendPaths(paths)
	if err != nil {
		return nil, err
	}
	summary, err := summarizePaths(normalized)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	service := s.transport
	summary["device_name"] = s.cfg.PeerName
	summary["instance_id"] = s.cfg.InstanceID
	settings := map[string]any{
		"rendezvous_url":  s.cfg.CrossNetwork.Wormhole.RendezvousURL,
		"transit_relay":   s.cfg.CrossNetwork.Wormhole.TransitRelay,
		"timeout_minutes": 10,
	}
	s.mu.Unlock()
	result, err := callTransport(service, "wormhole.create", map[string]any{
		"summary": summary, "settings": settings,
	})
	if err != nil {
		return nil, err
	}
	sessionID := stringValue(result["session_id"])
	if sessionID == "" {
		return nil, errors.New("一次性短码会话无效")
	}
	s.mu.Lock()
	s.pendingWormhole[sessionID] = normalized
	s.mu.Unlock()
	return result, nil
}

func summarizePaths(paths []string) (map[string]any, error) {
	itemCount := len(paths)
	fileCount := 0
	directoryCount := 0
	var totalBytes int64
	names := make([]string, 0, min(len(paths), 8))
	for _, path := range paths {
		info, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		if len(names) < 8 {
			names = append(names, filepath.Base(path))
		}
		if !info.IsDir() {
			fileCount++
			totalBytes += info.Size()
			continue
		}
		directoryCount++
		err = filepath.WalkDir(path, func(current string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.Type()&os.ModeSymlink != 0 {
				return fmt.Errorf("文件夹包含符号链接: %s", current)
			}
			if entry.Type().IsRegular() {
				info, err := entry.Info()
				if err != nil {
					return err
				}
				totalBytes += info.Size()
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return map[string]any{"item_count": itemCount, "file_count": fileCount,
		"directory_count": directoryCount, "total_bytes": totalBytes, "names": names}, nil
}

func (s *InkHoleService) JoinOneTime(code string) (map[string]any, error) {
	code = strings.TrimSpace(code)
	if code == "" {
		return nil, errors.New("请输入接收短码")
	}
	s.mu.Lock()
	service := s.transport
	settings := map[string]any{
		"rendezvous_url":  s.cfg.CrossNetwork.Wormhole.RendezvousURL,
		"transit_relay":   s.cfg.CrossNetwork.Wormhole.TransitRelay,
		"timeout_minutes": 10,
	}
	s.mu.Unlock()
	return callTransport(service, "wormhole.join.start", map[string]any{
		"code": code, "settings": settings,
	})
}

func (s *InkHoleService) AcceptOneTime(sessionID string) error {
	s.mu.Lock()
	service := s.transport
	s.mu.Unlock()
	_, err := callTransport(service, "wormhole.accept", map[string]any{"session_id": sessionID})
	return err
}

func (s *InkHoleService) RejectOneTime(sessionID string) error {
	s.mu.Lock()
	service := s.transport
	s.mu.Unlock()
	_, err := callTransport(service, "wormhole.reject", map[string]any{"session_id": sessionID})
	return err
}

func (s *InkHoleService) CancelTransportSession(sessionID string) error {
	s.mu.Lock()
	service := s.transport
	delete(s.pendingWormhole, sessionID)
	s.mu.Unlock()
	_, err := callTransport(service, "session.cancel", map[string]any{"session_id": sessionID})
	return err
}

func (s *InkHoleService) SaveWormholeConfig(rendezvousURL, transitRelay string) error {
	s.mu.Lock()
	if err := s.loadConfigLocked(); err != nil {
		s.mu.Unlock()
		return err
	}
	s.cfg.CrossNetwork.Wormhole.RendezvousURL = strings.TrimSpace(rendezvousURL)
	s.cfg.CrossNetwork.Wormhole.TransitRelay = strings.TrimSpace(transitRelay)
	err := s.saveConfigLocked()
	s.mu.Unlock()
	return err
}

func (s *InkHoleService) CrossNetworkConfig() (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.loadConfigLocked(); err != nil {
		return nil, err
	}
	profile := s.cfg.CrossNetwork.SSH.Profile
	privateKey, _ := s.credentials.Get(sshCredentialName(profile.ID, "private_key"))
	passphrase, _ := s.credentials.Get(sshCredentialName(profile.ID, "passphrase"))
	return map[string]any{
		"wormhole": s.cfg.CrossNetwork.Wormhole,
		"ssh": map[string]any{
			"enabled":       s.cfg.CrossNetwork.SSH.Enabled,
			"profile":       profile,
			"remotePort":    s.cfg.CrossNetwork.SSH.RemotePort,
			"peers":         s.cfg.CrossNetwork.SSH.Peers,
			"connected":     s.sshConnected,
			"hasPastedKey":  privateKey != "",
			"hasPassphrase": passphrase != "",
		},
	}, nil
}

func (s *InkHoleService) CheckSSH(input SSHSettingsInput) (map[string]any, error) {
	s.mu.Lock()
	if err := s.loadConfigLocked(); err != nil {
		s.mu.Unlock()
		return nil, err
	}
	service := s.transport
	profileID := s.cfg.CrossNetwork.SSH.Profile.ID
	s.mu.Unlock()
	profile, err := s.sshProfilePayload(profileID, input, true)
	if err != nil {
		return nil, err
	}
	return callTransport(service, "ssh.check", map[string]any{"profile": profile})
}

func (s *InkHoleService) SaveSSHConfig(input SSHSettingsInput) error {
	s.mu.Lock()
	if err := s.loadConfigLocked(); err != nil {
		s.mu.Unlock()
		return err
	}
	profileID := s.cfg.CrossNetwork.SSH.Profile.ID
	s.mu.Unlock()
	input.Host = strings.TrimSpace(input.Host)
	input.User = strings.TrimSpace(input.User)
	input.PrivateKeyPath = strings.TrimSpace(input.PrivateKeyPath)
	input.HostKeySHA256 = strings.TrimSpace(input.HostKeySHA256)
	if input.Port == 0 {
		input.Port = 22
	}
	if input.Enabled {
		if _, err := s.sshProfilePayload(profileID, input, true); err != nil {
			return err
		}
		if input.HostKeySHA256 == "" {
			return errors.New("请先检测并确认 SSH 主机指纹")
		}
	}
	if input.PastedKey != "" {
		if err := s.credentials.Set(sshCredentialName(profileID, "private_key"), input.PastedKey); err != nil {
			return fmt.Errorf("保存 SSH 私钥失败: %w", err)
		}
	}
	if input.ClearPassphrase {
		if err := s.credentials.Delete(sshCredentialName(profileID, "passphrase")); err != nil {
			return fmt.Errorf("清除 SSH 私钥口令失败: %w", err)
		}
	} else if input.Passphrase != "" {
		if err := s.credentials.Set(sshCredentialName(profileID, "passphrase"), input.Passphrase); err != nil {
			return fmt.Errorf("保存 SSH 私钥口令失败: %w", err)
		}
	}
	s.mu.Lock()
	s.cfg.CrossNetwork.SSH.Enabled = input.Enabled
	s.cfg.CrossNetwork.SSH.Profile = SSHProfileConfig{ID: profileID, Host: input.Host,
		Port: input.Port, User: input.User, PrivateKeyMode: input.PrivateKeyMode,
		PrivateKeyPath: input.PrivateKeyPath, HostKeySHA256: input.HostKeySHA256}
	if input.PrivateKeyMode == "paste" {
		s.cfg.CrossNetwork.SSH.Profile.PrivateKeyLabel = "已存入系统安全存储"
	}
	err := s.saveConfigLocked()
	running := s.running
	s.mu.Unlock()
	if err != nil {
		return err
	}
	if running {
		go func() {
			if restartErr := s.restartSSH(); restartErr != nil {
				s.emit("transport", map[string]any{"event": "ssh.config.error",
					"data": map[string]any{"error": restartErr.Error()}})
			}
		}()
	}
	return nil
}

func (s *InkHoleService) sshProfilePayload(profileID string,
	input SSHSettingsInput, allowOverrides bool) (map[string]any, error) {
	mode := input.PrivateKeyMode
	if mode != "paste" {
		mode = "file"
	}
	privateKey := ""
	if mode == "paste" {
		privateKey = input.PastedKey
		if privateKey == "" {
			privateKey, _ = s.credentials.Get(sshCredentialName(profileID, "private_key"))
		}
		if privateKey == "" {
			return nil, errors.New("请粘贴 SSH 私钥")
		}
	} else {
		path := filepath.Clean(input.PrivateKeyPath)
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() {
			return nil, errors.New("请选择有效的 SSH 私钥文件")
		}
		if info.Size() > 1024*1024 {
			return nil, errors.New("SSH 私钥文件过大")
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		privateKey = string(raw)
	}
	passphrase := input.Passphrase
	if passphrase == "" && (!allowOverrides || !input.ClearPassphrase) {
		passphrase, _ = s.credentials.Get(sshCredentialName(profileID, "passphrase"))
	}
	return map[string]any{"id": profileID, "host": strings.TrimSpace(input.Host),
		"port": input.Port, "user": strings.TrimSpace(input.User),
		"private_key": privateKey, "passphrase": passphrase,
		"private_key_label": filepath.Base(input.PrivateKeyPath),
		"host_key_sha256":   strings.TrimSpace(input.HostKeySHA256)}, nil
}

func (s *InkHoleService) configuredSSHInputLocked() SSHSettingsInput {
	profile := s.cfg.CrossNetwork.SSH.Profile
	return SSHSettingsInput{Enabled: s.cfg.CrossNetwork.SSH.Enabled,
		Host: profile.Host, Port: profile.Port, User: profile.User,
		PrivateKeyMode: profile.PrivateKeyMode, PrivateKeyPath: profile.PrivateKeyPath,
		HostKeySHA256: profile.HostKeySHA256}
}

// startSSH is serialized by sshMu: Start()'s background bring-up and a
// concurrent SaveSSHConfig restart would otherwise both call ssh.listen
// and leak whichever session loses the race.
func (s *InkHoleService) startSSH() error {
	s.sshMu.Lock()
	defer s.sshMu.Unlock()
	return s.startSSHSerialized()
}

func (s *InkHoleService) startSSHSerialized() error {
	s.mu.Lock()
	if !s.running || s.transport == nil || !s.cfg.CrossNetwork.SSH.Enabled {
		s.mu.Unlock()
		return nil
	}
	service := s.transport
	profileID := s.cfg.CrossNetwork.SSH.Profile.ID
	input := s.configuredSSHInputLocked()
	remotePort := s.cfg.CrossNetwork.SSH.RemotePort
	peers := append([]SSHPeerConfig(nil), s.cfg.CrossNetwork.SSH.Peers...)
	s.mu.Unlock()
	profile, err := s.sshProfilePayload(profileID, input, false)
	if err != nil {
		return err
	}
	noisePrivate, err := s.credentials.Get(sshCredentialName(profileID, "noise_private"))
	if err != nil {
		return fmt.Errorf("读取 SSH 端到端身份失败: %w", err)
	}
	if noisePrivate == "" && len(peers) > 0 {
		return errors.New("SSH 身份不可用，请删除已配对设备后重新配对")
	}
	result, err := callTransport(service, "ssh.listen", map[string]any{
		"profile": profile, "remote_port": remotePort,
		"noise_private": noisePrivate, "peers": peers,
	})
	if err != nil {
		return err
	}
	generated := stringValue(result["noise_private"])
	if generated != "" {
		if err := s.credentials.Set(sshCredentialName(profileID, "noise_private"), generated); err != nil {
			_, _ = callTransport(service, "session.cancel", map[string]any{
				"session_id": stringValue(result["session_id"])})
			return fmt.Errorf("保存 SSH 端到端身份失败: %w", err)
		}
	}
	runtimePeers := make(map[string]runtimeSSHPeer)
	if rawPeers, ok := result["peers"].([]any); ok {
		for _, raw := range rawPeers {
			peer := runtimePeer(raw)
			if peer.InstanceID != "" {
				runtimePeers[peer.InstanceID] = peer
			}
		}
	}
	s.mu.Lock()
	if s.transport != service {
		s.mu.Unlock()
		_, _ = callTransport(service, "session.cancel", map[string]any{
			"session_id": stringValue(result["session_id"])})
		return errors.New("跨网核心已经重启")
	}
	s.sshSessionID = stringValue(result["session_id"])
	s.sshConnected, _ = result["connected"].(bool)
	s.sshRuntime = runtimePeers
	if port, ok := numberValue(result["remote_port"]); ok {
		s.cfg.CrossNetwork.SSH.RemotePort = port
	}
	_ = s.saveConfigLocked()
	views := s.peerViewsLocked()
	s.mu.Unlock()
	s.emit("peers", views)
	s.emit("transport", map[string]any{"event": "ssh.ready", "data": result})
	return nil
}

func numberValue(value any) (int, bool) {
	switch typed := value.(type) {
	case float64:
		return int(typed), true
	case int:
		return typed, true
	case json.Number:
		result, err := strconv.Atoi(string(typed))
		return result, err == nil
	default:
		return 0, false
	}
}

func (s *InkHoleService) restartSSH() error {
	s.sshMu.Lock()
	defer s.sshMu.Unlock()
	s.mu.Lock()
	service := s.transport
	sessionID := s.sshSessionID
	s.sshSessionID = ""
	s.sshRuntime = make(map[string]runtimeSSHPeer)
	enabled := s.cfg.CrossNetwork.SSH.Enabled
	views := s.peerViewsLocked()
	s.mu.Unlock()
	if sessionID != "" {
		_, _ = callTransport(service, "session.cancel", map[string]any{"session_id": sessionID})
	}
	s.emit("peers", views)
	if enabled {
		return s.startSSHSerialized()
	}
	return nil
}

func (s *InkHoleService) rememberSSHPeer(peer runtimeSSHPeer) {
	if peer.InstanceID == "" || peer.RemotePort < 1 || peer.RemotePort > 65535 ||
		peer.NoisePublic == "" {
		return
	}
	s.mu.Lock()
	peer.EndToEnd = true
	s.sshRuntime[peer.InstanceID] = peer
	saved := SSHPeerConfig{ID: peer.ID, Name: peer.Name, InstanceID: peer.InstanceID,
		RemotePort: peer.RemotePort, NoisePublic: peer.NoisePublic, EndToEnd: true}
	updated := false
	for index := range s.cfg.CrossNetwork.SSH.Peers {
		if s.cfg.CrossNetwork.SSH.Peers[index].InstanceID == peer.InstanceID {
			s.cfg.CrossNetwork.SSH.Peers[index] = saved
			updated = true
			break
		}
	}
	if !updated {
		s.cfg.CrossNetwork.SSH.Peers = append(s.cfg.CrossNetwork.SSH.Peers, saved)
	}
	_ = s.saveConfigLocked()
	views := s.peerViewsLocked()
	s.mu.Unlock()
	s.emit("peers", views)
}

func (s *InkHoleService) CreateSSHPairing() (map[string]any, error) {
	s.mu.Lock()
	service, sessionID := s.transport, s.sshSessionID
	s.mu.Unlock()
	if sessionID == "" {
		return nil, errors.New("SSH 中继尚未连接")
	}
	return callTransport(service, "ssh.pair.create", map[string]any{"session_id": sessionID})
}

func (s *InkHoleService) JoinSSHPairing(code string) (map[string]any, error) {
	s.mu.Lock()
	service, sessionID := s.transport, s.sshSessionID
	s.mu.Unlock()
	if sessionID == "" {
		return nil, errors.New("SSH 中继尚未连接")
	}
	result, err := callTransport(service, "ssh.pair.join", map[string]any{
		"session_id": sessionID, "code": strings.TrimSpace(code),
	})
	if err == nil {
		if peer := runtimePeer(result["peer"]); peer.InstanceID != "" {
			s.rememberSSHPeer(peer)
		}
	}
	return result, err
}

func (s *InkHoleService) RemoveSSHPeer(instanceID string) error {
	s.mu.Lock()
	filtered := s.cfg.CrossNetwork.SSH.Peers[:0]
	for _, peer := range s.cfg.CrossNetwork.SSH.Peers {
		if peer.InstanceID != instanceID {
			filtered = append(filtered, peer)
		}
	}
	s.cfg.CrossNetwork.SSH.Peers = filtered
	delete(s.sshRuntime, instanceID)
	err := s.saveConfigLocked()
	running := s.running && s.cfg.CrossNetwork.SSH.Enabled
	s.mu.Unlock()
	if err != nil {
		return err
	}
	if running {
		return s.restartSSH()
	}
	return nil
}

func (s *InkHoleService) startEndpointSend(paths []string, target lan.SendTarget,
	after func()) (string, error) {
	normalized, err := normalizeSendPaths(paths)
	if err != nil {
		return "", err
	}
	s.mu.Lock()
	if !s.running || s.identity == nil || s.ctx == nil {
		s.mu.Unlock()
		return "", errors.New("墨洞未开启")
	}
	identity, selfID, secret := s.identity, s.cfg.InstanceID, s.secret
	ctx, cancel := context.WithCancel(s.ctx)
	sendID := fmt.Sprintf("send-%d", time.Now().UnixNano())
	s.sends[sendID] = cancel
	s.sendWG.Add(1)
	s.mu.Unlock()
	go func() {
		s.runSendBatch(ctx, sendID, normalized, []lan.SendTarget{target}, identity, selfID, secret)
		if after != nil {
			after()
		}
	}()
	return sendID, nil
}
