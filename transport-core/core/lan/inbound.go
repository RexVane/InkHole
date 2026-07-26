package lan

import (
	"crypto/subtle"
	"io"
	"net"
	"time"
)

// inboundHandshakeTimeout bounds the whole pre-dispatch phase: magic,
// optional IKCI ingress token and the WHPC answer. A connection that goes
// silent mid-handshake times out instead of parking a goroutine.
const inboundHandshakeTimeout = 15 * time.Second

// InboundConfig wires HandleInbound to one node's identity and receiver.
// PeerName and Capabilities are callbacks because shells may change them
// under their own locks while the listener keeps running.
type InboundConfig struct {
	// IngressToken authenticates loopback bridge traffic prefixed with
	// "IKCI"; empty rejects every IKCI connection.
	IngressToken string
	Identity     *Identity
	InstanceID   string
	PeerName     func() string
	Capabilities func() []string
	Receiver     *Receiver
}

// HandleInbound serves one raw inbound connection end to end: optional
// IKCI ingress authentication, then WHPC probe / WHPP transfer dispatch.
// It owns conn and always closes it. Both the JSON-RPC LAN session and
// the Wails desktop shell call this — the dispatch logic must not fork
// between shells again.
func HandleInbound(conn net.Conn, cfg InboundConfig) {
	_ = conn.SetDeadline(time.Now().Add(inboundHandshakeTimeout))
	head := make([]byte, 4)
	if _, err := io.ReadFull(conn, head); err != nil {
		_ = conn.Close()
		return
	}
	if string(head) == "IKCI" {
		if !authenticateIngress(conn, cfg.IngressToken) {
			_ = conn.Close()
			return
		}
		if _, err := io.ReadFull(conn, head); err != nil {
			_ = conn.Close()
			return
		}
	}
	switch string(head) {
	case "WHPC":
		_ = RespondProbe(conn, cfg.Identity, cfg.InstanceID,
			cfg.PeerName(), cfg.Capabilities())
		_ = conn.Close()
	case "WHPP":
		// HandleWHPP re-arms its own rolling idle deadline and closes conn.
		cfg.Receiver.HandleWHPP(conn)
	default:
		_ = conn.Close()
	}
}

func authenticateIngress(conn net.Conn, token string) bool {
	if token == "" || len(token) > 256 {
		return false
	}
	provided := make([]byte, len(token))
	if _, err := io.ReadFull(conn, provided); err != nil {
		return false
	}
	return subtle.ConstantTimeCompare(provided, []byte(token)) == 1
}
