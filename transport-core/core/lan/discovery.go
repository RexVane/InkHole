package lan

import (
	"context"
	"errors"
	"net"
	"sort"
	"strings"
	"sync"
	"time"
)

// Discovery glues the three candidate sources (mDNS, UDP broadcast, future
// manual entries) to WHPC verification and a parallel liveness prober, and
// exposes one verified peer list. Peers enter the list only after a signed
// WHPC exchange; they leave it only via the prober (or identity mismatch),
// with the tolerant timings the flapping diagnosis called for.

const (
	probeInterval = 5 * time.Second
	probeTimeout  = 3 * time.Second
	probeStrikes  = 4
	// maxConcurrentVerifications bounds the goroutines candidate
	// verification may spawn. Without it a hostile host can flood the
	// discovery port with announcements carrying ever-new instance ids,
	// each costing a TCP probe with a multi-second timeout.
	maxConcurrentVerifications = 8
)

// Config configures one LAN discovery node.
type Config struct {
	PeerName     string
	InstanceID   string
	Port         int
	Identity     *Identity
	Capabilities []string
	// LocalIPs to announce. Must not contain loopback unless empty networks
	// left nothing else (the caller decides; see the 127.0.0.1 diagnosis).
	LocalIPs []string
	// DisableMDNS/DisableBroadcast exist for tests and constrained setups.
	DisableMDNS      bool
	DisableBroadcast bool
	// Probe overrides for tests and special networks; zero = defaults.
	ProbeInterval time.Duration
	ProbeTimeout  time.Duration
	ProbeStrikes  int
}

// Peer is one verified LAN device.
type Peer struct {
	InstanceID   string
	Name         string
	Host         string
	Hosts        []string
	Port         int
	Capabilities []string
	PublicKey    string
	Fingerprint  string
	ServiceName  string
}

// Discovery runs the discovery/liveness stack for one node.
type Discovery struct {
	cfg      Config
	ctx      context.Context
	cancel   context.CancelFunc
	onPeers  func([]Peer)
	onStatus func(string)

	// Liveness knobs, overridable in tests; production uses the constants.
	interval time.Duration
	timeout  time.Duration
	strikes  int

	mu       sync.Mutex
	peers    map[string]*Peer // key: mDNS service name or "broadcast|<id>"
	strike   map[string]int
	verinstr map[string]bool // in-flight verification probes
	reported map[string]bool // identity errors already reported

	verifySem chan struct{}
	stopping  bool
	stopOnce  sync.Once

	broadcast *broadcaster
	mdns      *mdnsLayer
	wg        sync.WaitGroup
}

// Start brings up discovery. onPeers receives the full verified peer list
// after every change; onStatus receives human-readable status lines.
func Start(cfg Config, onPeers func([]Peer), onStatus func(string)) (*Discovery, error) {
	cfg.InstanceID = strings.ToLower(cfg.InstanceID)
	if !ValidInstanceID(cfg.InstanceID) {
		return nil, errInvalidInstanceID
	}
	if cfg.Identity == nil {
		return nil, errors.New("device identity is required")
	}
	if cfg.Port < 1 || cfg.Port > 65535 {
		return nil, errInvalidPort
	}
	if strings.TrimSpace(cfg.PeerName) == "" {
		return nil, errors.New("peer name is required")
	}
	ctx, cancel := context.WithCancel(context.Background())
	d := &Discovery{
		cfg:      cfg,
		ctx:      ctx,
		cancel:   cancel,
		onPeers:  onPeers,
		onStatus: onStatus,
		interval: cfg.ProbeInterval,
		timeout:  cfg.ProbeTimeout,
		strikes:  cfg.ProbeStrikes,
		peers:    make(map[string]*Peer),
		strike:   make(map[string]int),
		verinstr: map[string]bool{},
		reported: map[string]bool{},

		verifySem: make(chan struct{}, maxConcurrentVerifications),
	}
	if d.interval <= 0 {
		d.interval = probeInterval
	}
	if d.timeout <= 0 {
		d.timeout = probeTimeout
	}
	if d.strikes <= 0 {
		d.strikes = probeStrikes
	}
	if !cfg.DisableBroadcast {
		broadcast, err := newBroadcaster(cfg.InstanceID, cfg.Port,
			d.handleAnnouncement, d.knownLANHosts)
		if err != nil {
			d.status("热点设备发现启动失败: " + err.Error())
		} else {
			d.broadcast = broadcast
			d.wg.Add(1)
			go func() {
				defer d.wg.Done()
				broadcast.run(ctx)
			}()
		}
	}
	if !cfg.DisableMDNS {
		mdns, err := startMDNS(ctx, cfg, cfg.LocalIPs, d.handleEntry)
		if err != nil {
			d.Stop()
			return nil, err
		}
		d.mdns = mdns
	}
	d.wg.Add(1)
	go func() {
		defer d.wg.Done()
		d.probeLoop()
	}()
	return d, nil
}

// Stop tears the stack down and waits for every goroutine.
func (d *Discovery) Stop() {
	d.stopOnce.Do(func() {
		d.mu.Lock()
		d.stopping = true
		d.cancel()
		d.mu.Unlock()
		if d.mdns != nil {
			d.mdns.stop()
		}
		if d.broadcast != nil {
			d.broadcast.close()
		}
	})
	d.wg.Wait()
}

// Peers returns the current verified peer list, name-sorted.
func (d *Discovery) Peers() []Peer {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.snapshotLocked()
}

func (d *Discovery) snapshotLocked() []Peer {
	out := make([]Peer, 0, len(d.peers))
	for _, peer := range d.peers {
		out = append(out, *peer)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Name != out[j].Name {
			return out[i].Name < out[j].Name
		}
		return out[i].InstanceID < out[j].InstanceID
	})
	return out
}

func (d *Discovery) status(msg string) {
	if d.onStatus != nil {
		d.onStatus(msg)
	}
}

func (d *Discovery) emitPeers() {
	if d.onPeers == nil {
		return
	}
	d.mu.Lock()
	snapshot := d.snapshotLocked()
	d.mu.Unlock()
	d.onPeers(snapshot)
}

// knownLANHosts feeds verified addresses to the broadcaster so asymmetric
// hotspots (broadcast dropped one way) still converge, like p2p.py does.
func (d *Discovery) knownLANHosts() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	seen := make(map[string]bool)
	var out []string
	for _, peer := range d.peers {
		for _, host := range append([]string{peer.Host}, peer.Hosts...) {
			if host != "" && !seen[host] {
				seen[host] = true
				out = append(out, host)
			}
		}
	}
	return out
}

func (d *Discovery) handleAnnouncement(host string, announcement *Announcement) {
	key := "broadcast|" + announcement.InstanceID
	d.mu.Lock()
	for _, peer := range d.peers {
		if peer.InstanceID == announcement.InstanceID &&
			peer.Host == host && peer.Port == announcement.Port {
			d.mu.Unlock()
			return
		}
	}
	d.mu.Unlock()
	d.verifyCandidate(key, "", []string{host}, announcement.Port,
		announcement.InstanceID)
}

func (d *Discovery) handleEntry(entry mdnsEntry) {
	d.verifyCandidate(entry.ServiceName, entry.PeerName, entry.Hosts,
		entry.Port, entry.InstanceID)
}

// verifyCandidate probes a discovered candidate before it may enter the
// peer list; at most one verification per key runs at a time, and the
// total in flight is capped. A candidate that finds the cap exhausted is
// simply dropped — mDNS and the broadcast loop will offer it again.
func (d *Discovery) verifyCandidate(key, name string, hosts []string,
	port int, instanceID string) {
	d.mu.Lock()
	if d.stopping || d.verinstr[key] {
		d.mu.Unlock()
		return
	}
	select {
	case d.verifySem <- struct{}{}:
	default:
		d.mu.Unlock()
		return
	}
	d.verinstr[key] = true
	d.wg.Add(1)
	d.mu.Unlock()
	go func() {
		defer d.wg.Done()
		defer func() { <-d.verifySem }()
		defer func() {
			d.mu.Lock()
			delete(d.verinstr, key)
			d.mu.Unlock()
		}()
		result, connected := d.probeHosts(hosts, port, instanceID)
		if result == nil || d.ctx.Err() != nil {
			return
		}
		addresses := dedupeStrings(append([]string{connected}, hosts...))
		d.mu.Lock()
		if existingKey, existing := d.peerByInstanceLocked(result.InstanceID); existing != nil &&
			existingKey != key {
			// The same device already entered through the other discovery
			// source (mDNS vs broadcast). Merge addresses instead of showing
			// a duplicate entry.
			existing.Name = firstNonEmpty(result.PeerName, existing.Name)
			existing.Hosts = dedupeStrings(append(existing.Hosts, addresses...))
			existing.Capabilities = result.Capabilities
			existing.PublicKey = result.PublicKey
			existing.Fingerprint = result.Fingerprint
			delete(d.strike, existingKey)
		} else {
			d.peers[key] = &Peer{
				InstanceID:   result.InstanceID,
				Name:         firstNonEmpty(result.PeerName, name),
				Host:         connected,
				Hosts:        addresses,
				Port:         port,
				Capabilities: result.Capabilities,
				PublicKey:    result.PublicKey,
				Fingerprint:  result.Fingerprint,
				ServiceName:  key,
			}
			delete(d.strike, key)
			delete(d.reported, key)
		}
		d.mu.Unlock()
		d.emitPeers()
	}()
}

// peerByInstanceLocked finds the entry holding a verified instance id.
func (d *Discovery) peerByInstanceLocked(instanceID string) (string, *Peer) {
	for key, peer := range d.peers {
		if peer.InstanceID == instanceID {
			return key, peer
		}
	}
	return "", nil
}

// probeHosts tries every candidate address until one verifies. Identity
// mismatches are terminal for the whole candidate set.
func (d *Discovery) probeHosts(hosts []string, port int,
	expectedInstanceID string) (*ProbeResult, string) {
	for _, host := range hosts {
		if d.ctx.Err() != nil {
			return nil, ""
		}
		result, err := ProbePeer(host, port, d.timeout, expectedInstanceID)
		if err == nil {
			connected := result.ConnectedIP
			if connected == "" {
				connected = host
			}
			return result, connected
		}
		if errors.Is(err, ErrIdentityMismatch) {
			return nil, ""
		}
	}
	return nil, ""
}

// probeLoop is the authority on peer removal: every interval it reprobes
// all peers in parallel; probeStrikes consecutive full failures evict.
func (d *Discovery) probeLoop() {
	ticker := time.NewTicker(d.interval)
	defer ticker.Stop()
	for {
		select {
		case <-d.ctx.Done():
			return
		case <-ticker.C:
		}
		d.mu.Lock()
		type target struct {
			key  string
			peer Peer
		}
		targets := make([]target, 0, len(d.peers))
		for key, peer := range d.peers {
			targets = append(targets, target{key: key, peer: *peer})
		}
		d.mu.Unlock()
		if len(targets) == 0 {
			continue
		}
		type outcome struct {
			key      string
			result   *ProbeResult
			conn     string
			identity bool
		}
		results := make(chan outcome, len(targets))
		for _, item := range targets {
			go func(item target) {
				hosts := dedupeStrings(append([]string{item.peer.Host},
					item.peer.Hosts...))
				for _, host := range hosts {
					if d.ctx.Err() != nil {
						break
					}
					result, err := ProbePeer(host, item.peer.Port,
						d.timeout, item.peer.InstanceID)
					if err == nil {
						results <- outcome{key: item.key, result: result,
							conn: result.ConnectedIP}
						return
					}
					if errors.Is(err, ErrIdentityMismatch) {
						results <- outcome{key: item.key, identity: true}
						return
					}
				}
				results <- outcome{key: item.key}
			}(item)
		}
		changed := false
		for range targets {
			var out outcome
			select {
			case <-d.ctx.Done():
				return
			case out = <-results:
			}
			d.mu.Lock()
			peer, alive := d.peers[out.key]
			switch {
			case !alive:
				// Removed elsewhere while probing; nothing to do.
			case out.identity:
				delete(d.peers, out.key)
				delete(d.strike, out.key)
				changed = true
				if !d.reported[out.key] {
					d.reported[out.key] = true
					d.mu.Unlock()
					d.status(peer.Name + " 身份验证失败")
					d.mu.Lock()
				}
			case out.result != nil:
				peer.Name = firstNonEmpty(out.result.PeerName, peer.Name)
				peer.Capabilities = out.result.Capabilities
				peer.PublicKey = out.result.PublicKey
				peer.Fingerprint = out.result.Fingerprint
				if out.conn != "" && out.conn != peer.Host {
					peer.Host = out.conn
					peer.Hosts = dedupeStrings(
						append([]string{out.conn}, peer.Hosts...))
					changed = true
				}
				delete(d.strike, out.key)
			default:
				d.strike[out.key]++
				if d.strike[out.key] >= d.strikes {
					delete(d.peers, out.key)
					delete(d.strike, out.key)
					changed = true
				}
			}
			d.mu.Unlock()
		}
		if changed {
			d.emitPeers()
		}
	}
}

func dedupeStrings(values []string) []string {
	seen := make(map[string]bool, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	return out
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

// LocalIPv4s enumerates announceable IPv4 addresses: non-loopback,
// non-link-local, virtual interfaces excluded by the caller's judgment.
// Loopback never enters the list (see the 127.0.0.1 announcement bug).
func LocalIPv4s() []string {
	interfaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []string
	seen := make(map[string]bool)
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
			key := ip.String()
			if !seen[key] {
				seen[key] = true
				out = append(out, key)
			}
		}
	}
	return out
}
