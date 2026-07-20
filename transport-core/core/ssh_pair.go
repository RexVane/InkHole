package core

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/psanford/wormhole-william/wordlist"
	"golang.org/x/crypto/hkdf"
	"salsa.debian.org/vasudev/gospake2"
)

const sshPairingAppID = "com.rexvane.inkhole/ssh-pair-v1"

type sshSessionParams struct {
	SessionID string `json:"session_id"`
}

type joinSSHPairingParams struct {
	SessionID string `json:"session_id"`
	Code      string `json:"code"`
}

func (s *Service) createSSHPairing(raw json.RawMessage) (any, error) {
	var params sshSessionParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	current, ok := s.getSession(params.SessionID).(*sshListenerSession)
	if !ok {
		return nil, errors.New("SSH relay session not found")
	}
	code := fmt.Sprintf("%d-%s", current.remotePort, wordlist.ChooseWords(2))
	expires := time.Now().Add(10 * time.Minute)
	current.mu.Lock()
	current.pairCode = code
	current.pairExpiry = expires
	current.mu.Unlock()
	return map[string]any{
		"code":       code,
		"uri":        "inkhole://ssh-pair?code=" + code,
		"expires_at": expires.UTC().Format(time.RFC3339),
	}, nil
}

func (s *Service) joinSSHPairing(raw json.RawMessage) (any, error) {
	var params joinSSHPairingParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	current, ok := s.getSession(params.SessionID).(*sshListenerSession)
	if !ok {
		return nil, errors.New("SSH relay session not found")
	}
	code := strings.TrimSpace(params.Code)
	remotePort, err := pairingPort(code)
	if err != nil {
		return nil, err
	}
	current.mu.RLock()
	client := current.client
	current.mu.RUnlock()
	if client == nil {
		return nil, errors.New("SSH relay is reconnecting")
	}
	conn, err := client.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(remotePort)))
	if err != nil {
		return nil, fmt.Errorf("connect pairing channel: %w", err)
	}
	defer conn.Close()
	clearDeadline := setHandshakeDeadline(conn)
	defer clearDeadline()
	if _, err := conn.Write([]byte(sshModePair)); err != nil {
		return nil, err
	}
	peerIdentity, err := runPairInitiator(conn, code, current.identity)
	if err != nil {
		return nil, fmt.Errorf("SSH pairing failed: %w", err)
	}
	peer, err := current.addPeer(identityPeer(peerIdentity))
	if err != nil {
		return nil, err
	}
	s.emit("ssh.paired", map[string]any{"session_id": params.SessionID, "peer": peer})
	return map[string]any{"peer": peer}, nil
}

func pairingPort(code string) (int, error) {
	parts := strings.SplitN(strings.TrimSpace(code), "-", 2)
	if len(parts) != 2 || parts[1] == "" {
		return 0, errors.New("invalid SSH pairing code")
	}
	port, err := strconv.Atoi(parts[0])
	if err != nil || port < 1 || port > 65535 {
		return 0, errors.New("invalid SSH pairing code")
	}
	return port, nil
}

func (s *sshListenerSession) handlePair(conn net.Conn) {
	s.mu.RLock()
	code := s.pairCode
	expires := s.pairExpiry
	s.mu.RUnlock()
	if code == "" || time.Now().After(expires) {
		return
	}
	peerIdentity, err := runPairResponder(conn, code, s.identity)
	if err != nil {
		return
	}
	peer, err := s.addPeer(identityPeer(peerIdentity))
	if err != nil {
		return
	}
	s.mu.Lock()
	if s.pairCode == code {
		s.pairCode = ""
		s.pairExpiry = time.Time{}
	}
	s.mu.Unlock()
	s.service.emit("ssh.paired", map[string]any{"peer": peer})
}

func identityPeer(identity sshIdentity) SSHPeer {
	return SSHPeer{
		ID:          identity.InstanceID,
		Name:        identity.Name,
		InstanceID:  identity.InstanceID,
		RemotePort:  identity.RemotePort,
		NoisePublic: identity.NoisePublic,
		EndToEnd:    true,
	}
}

func runPairInitiator(conn net.Conn, code string, identity sshIdentity) (sshIdentity, error) {
	spake := gospake2.SPAKE2Symmetric(gospake2.NewPassword(code), gospake2.NewIdentityS(sshPairingAppID))
	ours := spake.Start()
	if err := writeFrame(conn, ours); err != nil {
		return sshIdentity{}, err
	}
	theirs, err := readFrame(conn, 4096)
	if err != nil {
		return sshIdentity{}, err
	}
	shared, err := spake.Finish(theirs)
	if err != nil {
		return sshIdentity{}, err
	}
	sealed, err := sealPairIdentity(shared, identity)
	if err != nil {
		return sshIdentity{}, err
	}
	if err := writeFrame(conn, sealed); err != nil {
		return sshIdentity{}, err
	}
	reply, err := readFrame(conn, 128*1024)
	if err != nil {
		return sshIdentity{}, err
	}
	return openPairIdentity(shared, reply)
}

func runPairResponder(conn net.Conn, code string, identity sshIdentity) (sshIdentity, error) {
	theirs, err := readFrame(conn, 4096)
	if err != nil {
		return sshIdentity{}, err
	}
	spake := gospake2.SPAKE2Symmetric(gospake2.NewPassword(code), gospake2.NewIdentityS(sshPairingAppID))
	ours := spake.Start()
	if err := writeFrame(conn, ours); err != nil {
		return sshIdentity{}, err
	}
	shared, err := spake.Finish(theirs)
	if err != nil {
		return sshIdentity{}, err
	}
	message, err := readFrame(conn, 128*1024)
	if err != nil {
		return sshIdentity{}, err
	}
	peer, err := openPairIdentity(shared, message)
	if err != nil {
		return sshIdentity{}, err
	}
	sealed, err := sealPairIdentity(shared, identity)
	if err != nil {
		return sshIdentity{}, err
	}
	if err := writeFrame(conn, sealed); err != nil {
		return sshIdentity{}, err
	}
	return peer, nil
}

func pairAEAD(shared []byte) (cipher.AEAD, error) {
	key := make([]byte, 32)
	if _, err := io.ReadFull(hkdf.New(sha256.New, shared, nil, []byte(sshPairingAppID+"/identity")), key); err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func sealPairIdentity(shared []byte, identity sshIdentity) ([]byte, error) {
	plain, err := json.Marshal(identity)
	if err != nil {
		return nil, err
	}
	aead, err := pairAEAD(shared)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	return append(nonce, aead.Seal(nil, nonce, plain, nil)...), nil
}

func openPairIdentity(shared, message []byte) (sshIdentity, error) {
	aead, err := pairAEAD(shared)
	if err != nil {
		return sshIdentity{}, err
	}
	if len(message) < aead.NonceSize()+aead.Overhead() {
		return sshIdentity{}, errors.New("invalid encrypted pairing identity")
	}
	nonce := message[:aead.NonceSize()]
	plain, err := aead.Open(nil, nonce, message[aead.NonceSize():], nil)
	if err != nil {
		return sshIdentity{}, errors.New("pairing code authentication failed")
	}
	var identity sshIdentity
	if err := json.Unmarshal(plain, &identity); err != nil {
		return sshIdentity{}, err
	}
	if strings.TrimSpace(identity.Name) == "" || strings.TrimSpace(identity.InstanceID) == "" ||
		identity.RemotePort < 1 || identity.RemotePort > 65535 {
		return sshIdentity{}, errors.New("paired device identity is invalid")
	}
	if _, err := decodeNoisePublic(identity.NoisePublic); err != nil {
		return sshIdentity{}, err
	}
	return identity, nil
}
