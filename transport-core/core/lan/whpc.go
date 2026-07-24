package lan

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"time"
)

// WHPC v3: a signed liveness/identity probe. The prober sends
// "WHPC" + nonce(32); the responder answers "WHPC" + len(u32 BE) + JSON
// {version, caps, instance_id, peer_name, public_key, signature} where
// signature covers CapabilityMessage(nonce, ...). Both sides of the
// exchange interoperate with the Python and Kotlin implementations.

const (
	capMagic = "WHPC"
	// CapVersion is the WHPC protocol version shared by all three stacks.
	CapVersion = 3
	// maxProbeBody mirrors _MAX_HEADER in p2p.py.
	maxProbeBody = 64 * 1024
)

// ErrIdentityMismatch marks a peer that answered correctly but failed the
// signature check or presented an unexpected instance id. Callers treat it
// as "remove this device", never as a transient network error.
var ErrIdentityMismatch = errors.New("device identity verification failed")

// ProbeResult is the verified answer of a WHPC v3 exchange.
type ProbeResult struct {
	InstanceID   string
	PeerName     string
	Capabilities []string
	ConnectedIP  string
	PublicKey    string
	Fingerprint  string
}

type probeBody struct {
	Version    int      `json:"version"`
	Caps       []string `json:"caps"`
	InstanceID string   `json:"instance_id"`
	PeerName   string   `json:"peer_name"`
	PublicKey  string   `json:"public_key"`
	Signature  string   `json:"signature"`
}

// ProbePeer dials host:port and runs one WHPC v3 challenge within timeout.
// A non-empty expectedInstanceID additionally pins the peer identity.
func ProbePeer(host string, port int, timeout time.Duration,
	expectedInstanceID string) (*ProbeResult, error) {
	conn, err := net.DialTimeout("tcp",
		net.JoinHostPort(host, strconv.Itoa(port)), timeout)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	nonce := make([]byte, nonceSize)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	if _, err := conn.Write(append([]byte(capMagic), nonce...)); err != nil {
		return nil, err
	}
	result, err := readProbeAnswer(conn, nonce)
	if err != nil {
		return nil, err
	}
	if expectedInstanceID != "" &&
		result.InstanceID != strings.ToLower(expectedInstanceID) {
		return nil, fmt.Errorf("%w: peer identity changed", ErrIdentityMismatch)
	}
	if addr, ok := conn.RemoteAddr().(*net.TCPAddr); ok {
		host := addr.IP.String()
		if zone := strings.IndexByte(host, '%'); zone >= 0 {
			host = host[:zone]
		}
		result.ConnectedIP = host
	}
	return result, nil
}

func readProbeAnswer(conn net.Conn, nonce []byte) (*ProbeResult, error) {
	head := make([]byte, 4)
	if _, err := io.ReadFull(conn, head); err != nil {
		return nil, fmt.Errorf("peer is not a WHPC v3 device: %w", err)
	}
	if string(head) != capMagic {
		return nil, errors.New("peer is not a WHPC v3 device")
	}
	if _, err := io.ReadFull(conn, head); err != nil {
		return nil, err
	}
	size := binary.BigEndian.Uint32(head)
	if size == 0 || size > maxProbeBody {
		return nil, errors.New("WHPC answer size is invalid")
	}
	raw := make([]byte, size)
	if _, err := io.ReadFull(conn, raw); err != nil {
		return nil, err
	}
	var body probeBody
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("WHPC answer is malformed: %w", err)
	}
	body.InstanceID = strings.ToLower(body.InstanceID)
	body.PeerName = strings.TrimSpace(body.PeerName)
	if body.Version != CapVersion || !ValidInstanceID(body.InstanceID) ||
		body.PeerName == "" || body.Caps == nil {
		return nil, errors.New("peer does not support WHPC v3")
	}
	fingerprint, err := PublicFingerprint(body.PublicKey)
	if err != nil {
		return nil, fmt.Errorf("%w: %s", ErrIdentityMismatch, err)
	}
	message, err := CapabilityMessage(
		nonce, body.InstanceID, body.PeerName, CapVersion, body.Caps)
	if err != nil {
		return nil, err
	}
	if !Verify(body.PublicKey, message, body.Signature) {
		return nil, fmt.Errorf("%w: bad signature", ErrIdentityMismatch)
	}
	return &ProbeResult{
		InstanceID:   body.InstanceID,
		PeerName:     body.PeerName,
		Capabilities: body.Caps,
		PublicKey:    body.PublicKey,
		Fingerprint:  fingerprint,
	}, nil
}

// RespondProbe answers one WHPC v3 challenge on conn. The caller has already
// consumed the 4-byte "WHPC" magic while dispatching the connection.
func RespondProbe(conn net.Conn, identity *Identity,
	instanceID, peerName string, capabilities []string) error {
	nonce := make([]byte, nonceSize)
	if _, err := io.ReadFull(conn, nonce); err != nil {
		return err
	}
	message, err := CapabilityMessage(
		nonce, instanceID, peerName, CapVersion, capabilities)
	if err != nil {
		return err
	}
	signature, err := identity.Sign(message)
	if err != nil {
		return err
	}
	raw, err := json.Marshal(probeBody{
		Version:    CapVersion,
		Caps:       capabilities,
		InstanceID: strings.ToLower(instanceID),
		PeerName:   peerName,
		PublicKey:  identity.PublicKey,
		Signature:  signature,
	})
	if err != nil {
		return err
	}
	out := make([]byte, 0, len(capMagic)+4+len(raw))
	out = append(out, capMagic...)
	out = binary.BigEndian.AppendUint32(out, uint32(len(raw)))
	out = append(out, raw...)
	_, err = conn.Write(out)
	return err
}
