package core

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

type SSHProfile struct {
	ID              string `json:"id"`
	Host            string `json:"host"`
	Port            int    `json:"port"`
	User            string `json:"user"`
	PrivateKey      string `json:"private_key"`
	PrivateKeyLabel string `json:"private_key_label,omitempty"`
	Passphrase      string `json:"passphrase,omitempty"`
	HostKeySHA256   string `json:"host_key_sha256,omitempty"`
}

type SSHPeer struct {
	ID            string `json:"id"`
	Name          string `json:"name"`
	InstanceID    string `json:"instance_id"`
	RemotePort    int    `json:"remote_port"`
	NoisePublic   string `json:"noise_public"`
	EndToEnd      bool   `json:"end_to_end"`
	Endpoint      string `json:"endpoint,omitempty"`
	EndpointToken string `json:"endpoint_token,omitempty"`
}

type sshCheckParams struct {
	Profile SSHProfile `json:"profile"`
}

func normalizeSSHProfile(profile *SSHProfile) error {
	profile.Host = strings.TrimSpace(profile.Host)
	profile.User = strings.TrimSpace(profile.User)
	profile.HostKeySHA256 = strings.TrimSpace(profile.HostKeySHA256)
	if profile.Port == 0 {
		profile.Port = 22
	}
	if profile.Host == "" || profile.User == "" || profile.PrivateKey == "" {
		return errors.New("SSH host, user and private key are required")
	}
	if profile.Port < 1 || profile.Port > 65535 {
		return errors.New("SSH port must be between 1 and 65535")
	}
	if strings.ContainsAny(profile.Host, "\r\n\x00") || strings.ContainsAny(profile.User, "\r\n\x00") {
		return errors.New("SSH host or user contains invalid characters")
	}
	return nil
}

func parseSSHSigner(profile SSHProfile) (ssh.Signer, error) {
	key := []byte(profile.PrivateKey)
	if profile.Passphrase != "" {
		signer, err := ssh.ParsePrivateKeyWithPassphrase(key, []byte(profile.Passphrase))
		if err != nil {
			return nil, fmt.Errorf("cannot unlock SSH private key: %w", err)
		}
		return signer, nil
	}
	signer, err := ssh.ParsePrivateKey(key)
	if err != nil {
		var missing *ssh.PassphraseMissingError
		if errors.As(err, &missing) {
			return nil, errors.New("SSH private key requires a passphrase")
		}
		return nil, fmt.Errorf("invalid SSH private key: %w", err)
	}
	return signer, nil
}

func dialSSH(ctx context.Context, profile SSHProfile, allowUnpinned bool) (*ssh.Client, string, error) {
	if err := normalizeSSHProfile(&profile); err != nil {
		return nil, "", err
	}
	signer, err := parseSSHSigner(profile)
	if err != nil {
		return nil, "", err
	}
	address := net.JoinHostPort(profile.Host, strconv.Itoa(profile.Port))
	var fingerprint string
	config := &ssh.ClientConfig{
		User:    profile.User,
		Auth:    []ssh.AuthMethod{ssh.PublicKeys(signer)},
		Timeout: 20 * time.Second,
		HostKeyCallback: func(_ string, _ net.Addr, key ssh.PublicKey) error {
			fingerprint = ssh.FingerprintSHA256(key)
			if profile.HostKeySHA256 == "" {
				if allowUnpinned {
					return nil
				}
				return errors.New("SSH host fingerprint has not been confirmed")
			}
			if subtle.ConstantTimeCompare([]byte(profile.HostKeySHA256), []byte(fingerprint)) != 1 {
				return fmt.Errorf("SSH host fingerprint changed: %s", fingerprint)
			}
			return nil
		},
	}
	raw, err := (&net.Dialer{Timeout: 20 * time.Second}).DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, fingerprint, fmt.Errorf("connect SSH server: %w", err)
	}
	_ = raw.SetDeadline(time.Now().Add(25 * time.Second))
	conn, channels, requests, err := ssh.NewClientConn(raw, address, config)
	if err != nil {
		_ = raw.Close()
		return nil, fingerprint, fmt.Errorf("SSH authentication failed: %w", err)
	}
	_ = raw.SetDeadline(time.Time{})
	return ssh.NewClient(conn, channels, requests), fingerprint, nil
}

func (s *Service) checkSSH(raw json.RawMessage) (any, error) {
	var params sshCheckParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	client, fingerprint, err := dialSSH(s.ctx, params.Profile, true)
	if err != nil {
		return nil, err
	}
	serverVersion := string(client.ServerVersion())
	_ = client.Close()
	return map[string]any{
		"fingerprint":    fingerprint,
		"server_version": serverVersion,
		"confirmed":      params.Profile.HostKeySHA256 == fingerprint,
	}, nil
}
