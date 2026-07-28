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
	// broadcastBurst is how many announcements one bump fires. Access points
	// routinely rate-limit or drop broadcast frames, so a single packet at
	// startup or after a Wi-Fi handover is a coin flip; five closely spaced
	// ones are not, and still cost well under a kilobyte.
	broadcastBurst    = 5
	broadcastBurstGap = 150 * time.Millisecond
)

// Announcement is one decoded discovery packet.
type Announcement struct {
	InstanceID string
	Port       int
	Reply      bool
	// Bye marks a departure notice sent as the node shuts down or leaves the
	// network. Protocol v3 peers that predate the field decode the packet as
	// an ordinary announcement and fall back to probing, so emitting it costs
	// nothing against older builds.
	Bye bool
}

type announcementJSON struct {
	Magic      string `json:"magic"`
	Version    int    `json:"version"`
	InstanceID string `json:"instance_id"`
	Port       int    `json:"port"`
	Reply      bool   `json:"reply"`
	Bye        bool   `json:"bye,omitempty"`
}

// EncodeAnnouncement mirrors _encode_lan_announcement.
func EncodeAnnouncement(instanceID string, port int, reply bool) ([]byte, error) {
	return encodeAnnouncement(instanceID, port, reply, false)
}

// EncodeGoodbye builds the departure notice described on Announcement.Bye.
func EncodeGoodbye(instanceID string, port int) ([]byte, error) {
	return encodeAnnouncement(instanceID, port, false, true)
}

func encodeAnnouncement(instanceID string, port int, reply, bye bool) ([]byte,
	error) {
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
		Bye:        bye,
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
		Bye:        decoded.Bye,
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
	// onBye receives departure notices so discovery can react without
	// waiting out the liveness prober.
	onBye func(host string, announcement *Announcement)
	// extraTargets supplies verified unicast addresses for asymmetric
	// hotspots that drop broadcasts in one direction.
	extraTargets func() []string
	// bumps requests an announcement burst; buffered so several triggers
	// arriving together collapse into one burst.
	bumps chan struct{}
}

func newBroadcaster(instanceID string, listenPort int,
	onPeer func(string, *Announcement), onBye func(string, *Announcement),
	extraTargets func() []string,
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
		onBye:        onBye,
		extraTargets: extraTargets,
		bumps:        make(chan struct{}, 1),
	}, nil
}

// bump asks the run loop to fire an announcement burst immediately.
func (b *broadcaster) bump() {
	select {
	case b.bumps <- struct{}{}:
	default:
	}
}

// sayGoodbye tells the segment we are leaving. Best effort by definition —
// a laptop whose Wi-Fi already dropped has nowhere to send it — which is why
// the prober remains the authority on removal.
func (b *broadcaster) sayGoodbye() {
	payload, err := EncodeGoodbye(b.instanceID, b.listenPort)
	if err != nil {
		return
	}
	b.sendToAll(payload)
}

func (b *broadcaster) close() {
	_ = b.sock.Close()
}

// sendToAll writes one payload to every directed broadcast address plus any
// verified unicast address discovery has learned.
func (b *broadcaster) sendToAll(payload []byte) {
	targets := broadcastTargets()
	if b.extraTargets != nil {
		targets = append(targets, b.extraTargets()...)
	}
	for _, target := range targets {
		addr, err := net.ResolveUDPAddr("udp4",
			net.JoinHostPort(target, strconv.Itoa(BroadcastPort)))
		if err != nil {
			continue
		}
		_, _ = b.sock.WriteToUDP(payload, addr)
	}
}

// run announces every broadcastInterval and answers announcements with a
// unicast reply, exactly like the Python and Kotlin loops. A bump collapses
// the schedule into a short burst. Returns when the context is cancelled or
// the socket is closed.
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
	burst := 0
	for ctx.Err() == nil {
		select {
		case <-b.bumps:
			burst = broadcastBurst
			nextAnnounce = time.Time{}
		default:
		}
		now := time.Now()
		if !now.Before(nextAnnounce) {
			b.sendToAll(announcement)
			if burst > 0 {
				burst--
				nextAnnounce = now.Add(broadcastBurstGap)
			} else {
				nextAnnounce = now.Add(broadcastInterval)
			}
		}
		// Never sleep past the next scheduled announcement, or a burst would
		// be stretched to the read timeout and stop being a burst.
		wait := broadcastReadTimeout
		if until := time.Until(nextAnnounce); until > 0 && until < wait {
			wait = until
		}
		_ = b.sock.SetReadDeadline(time.Now().Add(wait))
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
		if decoded.Bye {
			if b.onBye != nil {
				b.onBye(host, decoded)
			}
			continue
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
