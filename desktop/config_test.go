package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

type memoryCredentialStore struct {
	values map[string]string
	getErr error
}

func (m *memoryCredentialStore) Get(name string) (string, error) {
	if m.getErr != nil {
		return "", m.getErr
	}
	return m.values[name], nil
}

func (m *memoryCredentialStore) Has(name string) (bool, error) {
	if m.getErr != nil {
		return false, m.getErr
	}
	return m.values[name] != "", nil
}

func (m *memoryCredentialStore) Set(name, value string) error {
	if m.values == nil {
		m.values = make(map[string]string)
	}
	m.values[name] = value
	return nil
}

func (m *memoryCredentialStore) Delete(name string) error {
	delete(m.values, name)
	return nil
}

func useTemporaryConfigHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	if runtime.GOOS == "windows" {
		t.Setenv("AppData", filepath.Join(home, "AppData", "Roaming"))
	}
	return home
}

func TestConfigMigratesEarlyDesktopFieldsAndCredentials(t *testing.T) {
	useTemporaryConfigHome(t)
	path := configPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	source := map[string]any{
		"peer_name":        "迁移设备",
		"listen_port":      4242,
		"instance_id":      strings.Repeat("a", 32),
		"inbox":            filepath.Join(t.TempDir(), "received"),
		"identity_private": "private-identity",
		"secret":           "transfer-secret",
	}
	raw, err := json.Marshal(source)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}

	credentials := &memoryCredentialStore{values: make(map[string]string)}
	service := NewInkHoleService()
	service.credentials = credentials
	service.mu.Lock()
	err = service.loadConfigLocked()
	service.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	if service.cfg.PeerName != "迁移设备" || service.cfg.ListenPort != 4242 {
		t.Fatalf("migration produced name=%q port=%d", service.cfg.PeerName, service.cfg.ListenPort)
	}
	if !service.cfg.ShowPet {
		t.Fatal("show_pet should default to true when absent")
	}
	if !service.cfg.EncryptionEnabled {
		t.Fatal("legacy secret should migrate with encryption enabled")
	}
	if credentials.values[identityCredential] != "private-identity" ||
		credentials.values[transferCredential] != "transfer-secret" {
		t.Fatal("plaintext credentials were not moved to secure storage")
	}
	saved, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"identity_private", "\"secret\"", "peer_name", "listen_port"} {
		if strings.Contains(string(saved), forbidden) {
			t.Fatalf("saved config still contains %q", forbidden)
		}
	}
	// Windows reports synthetic POSIX mode bits (typically 0666); access is
	// controlled by ACLs and cannot be validated through os.FileMode.
	if runtime.GOOS != "windows" {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm()&0o077 != 0 {
			t.Fatalf("config permissions are too broad: %o", info.Mode().Perm())
		}
	}
}

func TestConfigPreservesExplicitHiddenPet(t *testing.T) {
	useTemporaryConfigHome(t)
	path := configPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	raw := []byte(`{"name":"测试设备","instance_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","show_pet":false}`)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	service := NewInkHoleService()
	service.credentials = &memoryCredentialStore{values: make(map[string]string)}
	service.mu.Lock()
	err := service.loadConfigLocked()
	service.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	if service.cfg.ShowPet {
		t.Fatal("explicit show_pet=false was not preserved")
	}
}

func TestConfigPreservesExplicitDisabledEncryptionWithStoredSecret(t *testing.T) {
	useTemporaryConfigHome(t)
	path := configPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	raw := []byte(`{"name":"测试设备","instance_id":"cccccccccccccccccccccccccccccccc","encryption_enabled":false}`)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	credentials := &memoryCredentialStore{values: map[string]string{
		transferCredential: "saved-secret",
	}}
	service := NewInkHoleService()
	service.credentials = credentials
	service.mu.Lock()
	err := service.loadConfigLocked()
	service.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	if service.cfg.EncryptionEnabled {
		t.Fatal("explicit encryption_enabled=false was not preserved")
	}
	if credentials.values[transferCredential] != "saved-secret" {
		t.Fatal("disabled encryption should retain the stored secret")
	}
}

func TestSaveConfigTogglesEncryptionWithoutDeletingSecret(t *testing.T) {
	useTemporaryConfigHome(t)
	inbox := filepath.Join(t.TempDir(), "inbox")
	credentials := &memoryCredentialStore{values: map[string]string{
		transferCredential: "saved-secret",
	}}
	service := NewInkHoleService()
	service.credentials = credentials

	if err := service.SaveConfig("测试设备", inbox, "", false, 0, true, false); err != nil {
		t.Fatal(err)
	}
	if service.cfg.EncryptionEnabled {
		t.Fatal("disabling encryption did not update the config")
	}
	if credentials.values[transferCredential] != "saved-secret" {
		t.Fatal("disabling encryption deleted the stored secret")
	}

	if err := service.SaveConfig("测试设备", inbox, "", false, 0, true, true); err != nil {
		t.Fatal(err)
	}
	if !service.cfg.EncryptionEnabled {
		t.Fatal("re-enabling encryption without re-entering the stored secret failed")
	}
}

func TestSaveConfigClearingSecretDisablesEncryption(t *testing.T) {
	useTemporaryConfigHome(t)
	credentials := &memoryCredentialStore{values: map[string]string{
		transferCredential: "saved-secret",
	}}
	service := NewInkHoleService()
	service.credentials = credentials

	if err := service.SaveConfig("测试设备", filepath.Join(t.TempDir(), "inbox"),
		"", true, 0, true, true); err != nil {
		t.Fatal(err)
	}
	if service.cfg.EncryptionEnabled {
		t.Fatal("clearing the secret did not force encryption off")
	}
	if credentials.values[transferCredential] != "" {
		t.Fatal("clearing the secret left it in credential storage")
	}
}

func TestEnabledEncryptionFailsClosedWhenCredentialReadFails(t *testing.T) {
	service := NewInkHoleService()
	service.credentials = &memoryCredentialStore{getErr: errors.New("credential store unavailable")}
	service.cfg.EncryptionEnabled = true

	secret, err := service.activeTransferSecretLocked()
	if err == nil {
		t.Fatal("enabled encryption silently degraded after credential read failure")
	}
	if secret != "" {
		t.Fatal("credential failure returned a transfer secret")
	}
	if !service.cfg.EncryptionEnabled {
		t.Fatal("credential failure permanently disabled encryption")
	}
}

func TestCompareVersions(t *testing.T) {
	tests := []struct {
		left, right string
		want        int
	}{
		{"1.6.9", "1.6.8", 1},
		{"v1.6.8", "1.6.8", 0},
		{"1.6.8", "1.7.0", -1},
		{"2.0", "1.99.99", 1},
		{"1.6.8-beta.1", "1.6.8", 0},
	}
	for _, test := range tests {
		got := compareVersions(test.left, test.right)
		if got != test.want {
			t.Errorf("compareVersions(%q, %q) = %d, want %d", test.left, test.right, got, test.want)
		}
	}
}
