package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/google/uuid"
	"github.com/rexvane/inkhole/transport-core/core/lan"
)

const (
	appVersion         = "1.7.3"
	identityCredential = "lan_identity_p256"
	transferCredential = "transfer_secret_v1"
	maximumRecentFiles = 50
	maximumManualPeers = 32
)

type ManualPeerConfig struct {
	Name        string `json:"name"`
	Host        string `json:"host"`
	Port        int    `json:"port"`
	InstanceID  string `json:"instance_id,omitempty"`
	Fingerprint string `json:"fingerprint,omitempty"`
}

type WormholeConfig struct {
	RendezvousURL string `json:"rendezvous_url,omitempty"`
	TransitRelay  string `json:"transit_relay,omitempty"`
}

type SSHProfileConfig struct {
	ID              string `json:"id"`
	Host            string `json:"host"`
	Port            int    `json:"port"`
	User            string `json:"user"`
	PrivateKeyMode  string `json:"private_key_mode"`
	PrivateKeyPath  string `json:"private_key_path,omitempty"`
	PrivateKeyLabel string `json:"private_key_label,omitempty"`
	HostKeySHA256   string `json:"host_key_sha256,omitempty"`
}

type SSHPeerConfig struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	InstanceID  string `json:"instance_id"`
	RemotePort  int    `json:"remote_port"`
	NoisePublic string `json:"noise_public"`
	EndToEnd    bool   `json:"end_to_end"`
}

type SSHConfig struct {
	Enabled    bool             `json:"enabled"`
	Profile    SSHProfileConfig `json:"profile"`
	RemotePort int              `json:"remote_port"`
	Peers      []SSHPeerConfig  `json:"peers,omitempty"`
}

type CrossNetworkConfig struct {
	Wormhole WormholeConfig `json:"wormhole"`
	SSH      SSHConfig      `json:"ssh"`
}

type Config struct {
	PeerName          string             `json:"name"`
	InstanceID        string             `json:"instance_id"`
	Inbox             string             `json:"inbox"`
	ListenPort        int                `json:"port"`
	EncryptionEnabled bool               `json:"encryption_enabled"`
	ManualPeers       []ManualPeerConfig `json:"manual_peers,omitempty"`
	RecentFiles       []string           `json:"recent_files,omitempty"`
	CrossNetwork      CrossNetworkConfig `json:"cross_network"`
	ShowPet           bool               `json:"show_pet"`
}

func configPath() string {
	base, err := os.UserConfigDir()
	if err != nil {
		base = "."
	}
	return filepath.Join(base, "InkHole", "desktop.json")
}

func legacyConfigPath() string {
	base, err := os.UserConfigDir()
	if err != nil {
		return ""
	}
	return filepath.Join(base, "InkHole", "config.json")
}

func defaultInbox() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join("InkHole", "收件箱")
	}
	if runtime.GOOS == "darwin" {
		return filepath.Join(home, "Documents", "inkhole")
	}
	if runtime.GOOS == "windows" {
		return filepath.Join(home, "Desktop", "inkhole")
	}
	return filepath.Join(home, "InkHole", "收件箱")
}

func newProfileID() string {
	raw := make([]byte, 12)
	if _, err := rand.Read(raw); err == nil {
		return hex.EncodeToString(raw)
	}
	return strings.ReplaceAll(uuid.NewString(), "-", "")[:24]
}

func (s *InkHoleService) loadConfigLocked() error {
	if s.configLoaded {
		return nil
	}
	path := configPath()
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		legacy := legacyConfigPath()
		if legacy != "" {
			raw, err = os.ReadFile(legacy)
		}
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("读取设置失败: %w", err)
	}
	var source map[string]any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &source); err != nil {
			return fmt.Errorf("设置文件格式无效: %w", err)
		}
		normalized, err := json.Marshal(source)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(normalized, &s.cfg); err != nil {
			return fmt.Errorf("设置内容无效: %w", err)
		}
	}
	if source == nil {
		source = make(map[string]any)
	}
	if _, exists := source["name"]; !exists {
		if value, ok := source["peer_name"].(string); ok {
			s.cfg.PeerName = value
		}
	}
	if _, exists := source["port"]; !exists {
		if value, ok := source["listen_port"].(float64); ok {
			s.cfg.ListenPort = int(value)
		}
	}
	if _, exists := source["show_pet"]; !exists {
		s.cfg.ShowPet = true
	}
	s.migratePlaintextCredential(source, "identity_private", identityCredential, &s.legacyIdentity)
	s.migratePlaintextCredential(source, "secret", transferCredential, &s.legacySecret)
	// Before the explicit toggle existed, having a transfer secret always
	// meant encryption was enabled. Preserve that behavior during migration;
	// an explicit false remains authoritative and keeps the secret dormant.
	if _, exists := source["encryption_enabled"]; !exists {
		storedSecret, err := s.credentials.Has(transferCredential)
		if s.legacySecret != "" || (err == nil && storedSecret) {
			s.cfg.EncryptionEnabled = true
		}
	}
	s.normalizeConfigLocked()
	s.configLoaded = true
	return s.saveConfigLocked()
}

func (s *InkHoleService) migratePlaintextCredential(
	source map[string]any, field, credential string, fallback *string,
) {
	value, _ := source[field].(string)
	value = strings.TrimSpace(value)
	if value == "" {
		return
	}
	if err := s.credentials.Set(credential, value); err != nil {
		*fallback = value
		s.credentialWarning = "系统安全存储不可用；敏感设置仅在本次运行中生效"
	}
}

func (s *InkHoleService) normalizeConfigLocked() {
	if !lan.ValidInstanceID(strings.ToLower(s.cfg.InstanceID)) {
		s.cfg.InstanceID = strings.ReplaceAll(uuid.NewString(), "-", "")
	} else {
		s.cfg.InstanceID = strings.ToLower(s.cfg.InstanceID)
	}
	s.cfg.PeerName = strings.TrimSpace(s.cfg.PeerName)
	if s.cfg.PeerName == "" {
		s.cfg.PeerName, _ = os.Hostname()
		if s.cfg.PeerName == "" {
			s.cfg.PeerName = "墨洞设备"
		}
	}
	if len([]rune(s.cfg.PeerName)) > 40 {
		s.cfg.PeerName = string([]rune(s.cfg.PeerName)[:40])
	}
	if strings.TrimSpace(s.cfg.Inbox) == "" {
		s.cfg.Inbox = defaultInbox()
	}
	s.cfg.Inbox = filepath.Clean(s.cfg.Inbox)
	if s.cfg.ListenPort < 0 || s.cfg.ListenPort > 65535 {
		s.cfg.ListenPort = 0
	}
	if len(s.cfg.RecentFiles) > maximumRecentFiles {
		s.cfg.RecentFiles = s.cfg.RecentFiles[:maximumRecentFiles]
	}
	if len(s.cfg.ManualPeers) > maximumManualPeers {
		s.cfg.ManualPeers = s.cfg.ManualPeers[:maximumManualPeers]
	}
	if s.cfg.CrossNetwork.SSH.Profile.ID == "" {
		s.cfg.CrossNetwork.SSH.Profile.ID = newProfileID()
	}
	profile := &s.cfg.CrossNetwork.SSH.Profile
	if profile.Port < 1 || profile.Port > 65535 {
		profile.Port = 22
	}
	if profile.PrivateKeyMode != "paste" {
		profile.PrivateKeyMode = "file"
	}
	if s.cfg.CrossNetwork.SSH.RemotePort < 0 || s.cfg.CrossNetwork.SSH.RemotePort > 65535 {
		s.cfg.CrossNetwork.SSH.RemotePort = 0
	}
	for index := range s.cfg.CrossNetwork.SSH.Peers {
		s.cfg.CrossNetwork.SSH.Peers[index].EndToEnd = true
	}
}

func (s *InkHoleService) saveConfigLocked() error {
	path := configPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(s.cfg, "", "  ")
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".desktop-*.json")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	cleanup := func() {
		_ = temporary.Close()
		_ = os.Remove(temporaryPath)
	}
	if err := temporary.Chmod(0o600); err != nil {
		cleanup()
		return err
	}
	if _, err := temporary.Write(raw); err != nil {
		cleanup()
		return err
	}
	if err := temporary.Sync(); err != nil {
		cleanup()
		return err
	}
	if err := temporary.Close(); err != nil {
		_ = os.Remove(temporaryPath)
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		_ = os.Remove(temporaryPath)
		return err
	}
	return nil
}
