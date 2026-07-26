//go:build windows

package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
)

type systemCredentialStore struct{}

func (systemCredentialStore) Has(name string) (bool, error) {
	path, err := windowsCredentialPath(name)
	if err != nil {
		return false, err
	}
	_, err = os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

func windowsCredentialPath(name string) (string, error) {
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(credentialService + "\x00" + name))
	return filepath.Join(base, "InkHole", "credentials", hex.EncodeToString(digest[:])+".bin"), nil
}

func windowsPowerShell(script, path, value string) ([]byte, error) {
	command := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script)
	command.Env = append(os.Environ(), "INKHOLE_CREDENTIAL_PATH="+path,
		"INKHOLE_CREDENTIAL_VALUE="+value)
	return command.Output()
}

func (systemCredentialStore) Get(name string) (string, error) {
	path, err := windowsCredentialPath(name)
	if err != nil {
		return "", err
	}
	script := `$p=$env:INKHOLE_CREDENTIAL_PATH; if (!(Test-Path -LiteralPath $p)) { exit 44 }; ` +
		`$b=[IO.File]::ReadAllBytes($p); $d=[Security.Cryptography.ProtectedData]::Unprotect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser); ` +
		`[Console]::Out.Write([Text.Encoding]::UTF8.GetString($d))`
	output, err := windowsPowerShell(script, path, "")
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && exitErr.ExitCode() == 44 {
			return "", nil
		}
		return "", err
	}
	return string(output), nil
}

func (systemCredentialStore) Set(name, value string) error {
	if value == "" {
		return systemCredentialStore{}.Delete(name)
	}
	path, err := windowsCredentialPath(name)
	if err != nil {
		return err
	}
	script := `$p=$env:INKHOLE_CREDENTIAL_PATH; $v=$env:INKHOLE_CREDENTIAL_VALUE; ` +
		`[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($p)) | Out-Null; ` +
		`$b=[Text.Encoding]::UTF8.GetBytes($v); $e=[Security.Cryptography.ProtectedData]::Protect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser); [IO.File]::WriteAllBytes($p,$e)`
	_, err = windowsPowerShell(script, path, value)
	return err
}

func (systemCredentialStore) Delete(name string) error {
	path, err := windowsCredentialPath(name)
	if err != nil {
		return err
	}
	err = os.Remove(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}
