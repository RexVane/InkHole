package lan

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var (
	errInvalidInstanceID = errors.New("instance id must be 32 lowercase hex chars")
	errInvalidPort       = errors.New("port must be in 1..65535")
	errNotUDP            = errors.New("listener is not a UDP socket")
)

// UDP broadcast discovery, wire-compatible with p2p.py and LanDiscovery.kt:
// JSON {"magic":"inkhole-lan-v1","version":3,"instance_id":...,"port":...,
// "reply":bool} on port 41301. Phone hotspots frequently refuse to forward
// mDNS between the AP and tethered clients; this layer survives them.

const (
	BroadcastPort        = 41301
	broadcastMagic       = "inkhole-lan-v1"
	broadcastVersion     = 3
	broadcastMaxPacket   = 2048
	broadcastInterval    = 2 * time.Second
	broadcastReadTimeout = 500 * time.Millisecond
)

// Announcement is one decoded discovery packet.
type Announcement struct {
	InstanceID string
	Port       int
	Reply      bool
}

type announcementJSON struct {
	Magic      string `json:"magic"`
	Version    int    `json:"version"`
	InstanceID string `json:"instance_id"`
	Port       int    `json:"port"`
	Reply      bool   `json:"reply"`
}

// EncodeAnnouncement mirrors _encode_lan_announcement.
func EncodeAnnouncement(instanceID string, port int, reply bool) ([]byte, error) {
	if !ValidInstanceID(strings.ToLower(instanceID)) {
		return nil, errInvalidInstanceID
	}
	if port < 1 || port > 65535 {
		return nil, errInvalidPort
	}
	return json.Marshal(announcementJSON{
		Magic:      broadcastMagic,
		Version:    broadcastVersion,
		InstanceID: strings.ToLower(instanceID),
		Port:       port,
		Reply:      reply,
	})
}

// DecodeAnnouncement mirrors _decode_lan_announcement; returns nil for
// anything malformed or from a different protocol version.
func DecodeAnnouncement(payload []byte) *Announcement {
	if len(payload) == 0 || len(payload) > broadcastMaxPacket {
		return nil
	}
	var decoded announcementJSON
	if err := json.Unmarshal(payload, &decoded); err != nil {
		return nil
	}
	decoded.InstanceID = strings.ToLower(decoded.InstanceID)
	if decoded.Magic != broadcastMagic || decoded.Version != broadcastVersion ||
		!ValidInstanceID(decoded.InstanceID) ||
		decoded.Port < 1 || decoded.Port > 65535 {
		return nil
	}
	return &Announcement{
		InstanceID: decoded.InstanceID,
		Port:       decoded.Port,
		Reply:      decoded.Reply,
	}
}

// broadcastTargets returns the directed broadcast address of every non-
// virtual IPv4 network plus the limited broadcast address.
func broadcastTargets() []string {
	targets := []string{"255.255.255.255"}
	interfaces, err := net.Interfaces()
	if err != nil {
		return targets
	}
	seen := map[string]bool{"255.255.255.255": true}
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipNet.IP.To4()
			if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
				continue
			}
			ones, bits := ipNet.Mask.Size()
			if bits != 32 || ones < 1 || ones > 31 {
				continue
			}
			broadcast := make(net.IP, 4)
			for i := range broadcast {
				broadcast[i] = ip[i] | ^ipNet.Mask[i]
			}
			key := broadcast.String()
			if !seen[key] {
				seen[key] = true
				targets = append(targets, key)
			}
		}
	}
	return targets
}

// broadcaster owns the UDP socket for announce/reply exchange.
type broadcaster struct {
	sock       *net.UDPConn
	instanceID string
	listenPort int
	// onPeer receives every remote announcement (already self-filtered).
	onPeer func(host string, announcement *Announcement)
	// extraTargets supplies verified unicast addresses for asymmetric
	// hotspots that drop broadcasts in one direction.
	extraTargets func() []string
}

func newBroadcaster(instanceID string, listenPort int,
	onPeer func(string, *Announcement), extraTargets func() []string,
) (*broadcaster, error) {
	config := net.ListenConfig{Control: reusePortControl}
	packet, err := config.ListenPacket(context.Background(), "udp4",
		":"+strconv.Itoa(BroadcastPort))
	if err != nil {
		return nil, err
	}
	sock, ok := packet.(*net.UDPConn)
	if !ok {
		_ = packet.Close()
		return nil, errNotUDP
	}
	return &broadcaster{
		sock:         sock,
		instanceID:   strings.ToLower(instanceID),
		listenPort:   listenPort,
		onPeer:       onPeer,
		extraTargets: extraTargets,
	}, nil
}

func (b *broadcaster) close() {
	_ = b.sock.Close()
}

// run announces every broadcastInterval and answers announcements with a
// unicast reply, exactly like the Python and Kotlin loops. Returns when the
// context is cancelled or the socket is closed.
func (b *broadcaster) run(ctx context.Context) {
	announcement, err := EncodeAnnouncement(b.instanceID, b.listenPort, false)
	if err != nil {
		return
	}
	reply, _ := EncodeAnnouncement(b.instanceID, b.listenPort, true)
	stop := context.AfterFunc(ctx, func() { _ = b.sock.Close() })
	defer stop()
	buf := make([]byte, broadcastMaxPacket+1)
	var nextAnnounce time.Time
	for ctx.Err() == nil {
		now := time.Now()
		if !now.Before(nextAnnounce) {
			targets := broadcastTargets()
			if b.extraTargets != nil {
				for _, extra := range b.extraTargets() {
					targets = append(targets, extra)
				}
			}
			for _, target := range targets {
				addr, err := net.ResolveUDPAddr("udp4",
					net.JoinHostPort(target, strconv.Itoa(BroadcastPort)))
				if err != nil {
					continue
				}
				_, _ = b.sock.WriteToUDP(announcement, addr)
			}
			nextAnnounce = now.Add(broadcastInterval)
		}
		_ = b.sock.SetReadDeadline(now.Add(broadcastReadTimeout))
		n, sender, err := b.sock.ReadFromUDP(buf)
		if err != nil {
			var netErr net.Error
			if errors.As(err, &netErr) && netErr.Timeout() {
				continue
			}
			return
		}
		if n <= 0 || n > broadcastMaxPacket {
			continue
		}
		decoded := DecodeAnnouncement(buf[:n])
		if decoded == nil || decoded.InstanceID == b.instanceID {
			continue
		}
		host := sender.IP.String()
		if zone := strings.IndexByte(host, '%'); zone >= 0 {
			host = host[:zone]
		}
		if !decoded.Reply {
			_, _ = b.sock.WriteToUDP(reply, &net.UDPAddr{
				IP: sender.IP, Port: BroadcastPort})
		}
		if b.onPeer != nil {
			b.onPeer(host, decoded)
		}
	}
}

// reusePortControl lets several nodes on one machine share the discovery
// port (tests, desktop + CLI side by side), matching SO_REUSEADDR/REUSEPORT
// in the Python and Kotlin implementations.
func reusePortControl(network, address string, conn syscall.RawConn) error {
	var controlErr error
	err := conn.Control(func(fd uintptr) {
		controlErr = setReuseSocketOptions(fd)
	})
	if err != nil {
		return err
	}
	return controlErr
}
