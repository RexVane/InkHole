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

	// lanProbeTimeout is the connect budget for a peer that shares one of our
	// own subnets. Such a peer answers in single-digit milliseconds, so the
	// tolerant remote budget bought nothing and cost a lot: a peer carrying
	// two stale addresses burned six seconds before the live one was tried.
	// Peers reached over Tailscale or a sleeping phone keep the full
	// probeTimeout, which is what stopped the original flapping.
	lanProbeTimeout = 700 * time.Millisecond
	// activeWindow is how long discovery runs hot after a start, a network
	// change or an explicit Refresh. Long enough to cover a Wi-Fi handover,
	// short enough that idle machines fall back to the quiet cadence.
	activeWindow        = 6 * time.Second
	activeProbeInterval = 800 * time.Millisecond
	// idleQueryInterval paces mDNS sweeps once the active window closes.
	idleQueryInterval = 8 * time.Second
	// netPollInterval bounds how long a Wi-Fi switch can go unnoticed. While
	// interface enumeration is cheap, frequent polling increases CPU wakeups
	// and power consumption on battery devices. 2-second polling provides
	// acceptable latency while minimizing resource usage.
	netPollInterval = 2 * time.Second
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

	// localNets is the set of IPv4 networks this host currently sits on,
	// refreshed by netmonLoop. It decides which peers get the fast LAN
	// probe budget and which ones a lost subnet makes instantly unreachable.
	localNets []*net.IPNet
	// activeUntil marks the end of the current hot window.
	activeUntil time.Time
	// wake nudges the probe loop out of its wait; buffered so a burst of
	// triggers collapses into one extra round.
	wake chan struct{}
	// queryWake nudges the mDNS query loop out of its wait
	queryWake chan struct{}

	verifySem chan struct{}
	stopping  bool
	stopOnce  sync.Once

	// goodbyeThrottle prevents DoS via rapid goodbye messages
	goodbyeHandling map[string]time.Time

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

		localNets:       localNetworks(),
		wake:            make(chan struct{}, 1),
		queryWake:       make(chan struct{}, 1),
		goodbyeHandling: make(map[string]time.Time),

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
			d.handleAnnouncement, d.handleGoodbye, d.knownLANHosts)
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
		d.wg.Add(1)
		go func() {
			defer d.wg.Done()
			d.queryLoop()
		}()
	}
	d.wg.Add(1)
	go func() {
		defer d.wg.Done()
		d.netmonLoop()
	}()
	d.wg.Add(1)
	go func() {
		defer d.wg.Done()
		d.probeLoop()
	}()
	// Start hot: the first seconds after launch are exactly when the user is
	// staring at an empty device list.
	d.Refresh()
	return d, nil
}

// Stop tears the stack down and waits for every goroutine.
func (d *Discovery) Stop() {
	d.stopOnce.Do(func() {
		// Announce departure while the socket and the network are still up,
		// so peers drop us in milliseconds instead of waiting out four
		// strikes. Ordering matters: cancelling the context closes the socket.
		if d.broadcast != nil {
			d.broadcast.sayGoodbye()
		}
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
	// Our own registration comes back on both the browse channel and the
	// active query sweep; without this the node verifies itself, succeeds,
	// and lists itself as a peer.
	if entry.InstanceID == d.cfg.InstanceID {
		return
	}
	d.verifyCandidate(entry.ServiceName, entry.PeerName, entry.Hosts,
		entry.Port, entry.InstanceID)
}

// handleGoodbye reacts to a departure notice. Anyone on the segment can forge
// one, so it is a hint rather than an order: confirm with a probe, and only
// then drop the device. That still retires a peer in a few hundred
// milliseconds instead of the four strikes silence would have cost.
func (d *Discovery) handleGoodbye(host string, announcement *Announcement) {
	d.mu.Lock()
	if d.stopping {
		d.mu.Unlock()
		return
	}

	key, peer := d.peerByInstanceLocked(announcement.InstanceID)
	if peer == nil {
		// Unknown peer - don't add to throttle map to prevent DoS
		d.mu.Unlock()
		return
	}
	snapshot := *peer
	knownHosts := dedupeStrings(append([]string{snapshot.Host},
		snapshot.Hosts...))
	if !stringInSlice(host, knownHosts) {
		// A goodbye is unauthenticated UDP. Only accept it from an address
		// already tied to this signed WHPC identity.
		d.mu.Unlock()
		return
	}

	// Throttle goodbye handling to prevent DoS via rapid goodbye flooding.
	// Allow at most one goodbye per instance per 500ms.
	if last, ok := d.goodbyeHandling[announcement.InstanceID]; ok {
		if time.Since(last) < 500*time.Millisecond {
			d.mu.Unlock()
			return
		}
	}
	d.goodbyeHandling[announcement.InstanceID] = time.Now()

	nets := append([]*net.IPNet(nil), d.localNets...)
	d.wg.Add(1)
	d.mu.Unlock()
	go func() {
		defer d.wg.Done()
		_, _, verdict := d.probePeerHosts(knownHosts, snapshot.Port,
			snapshot.InstanceID, nets)
		if (verdict != hostGone && verdict != hostMismatch) ||
			d.ctx.Err() != nil {
			// Silence is still ambiguous: an asleep phone produces the same
			// timeout as a departed one. Let the normal strike policy decide.
			return
		}
		d.mu.Lock()
		current, ok := d.peers[key]
		if !ok || current.InstanceID != snapshot.InstanceID {
			d.mu.Unlock()
			return
		}
		delete(d.peers, key)
		delete(d.strike, key)
		delete(d.goodbyeHandling, snapshot.InstanceID)
		d.mu.Unlock()
		d.emitPeers()
	}()
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

// probeHosts races every candidate address; the first that verifies wins.
// Identity mismatches are terminal for the whole candidate set.
func (d *Discovery) probeHosts(hosts []string, port int,
	expectedInstanceID string) (*ProbeResult, string) {
	d.mu.Lock()
	nets := append([]*net.IPNet(nil), d.localNets...)
	d.mu.Unlock()
	result, connected, verdict := d.probePeerHosts(hosts, port,
		expectedInstanceID, nets)
	if verdict != hostAlive {
		return nil, ""
	}
	return result, connected
}

// Refresh drives discovery hard for the next few seconds: it re-arms the
// active window, fires an announcement burst, wakes the prober and lets the
// mDNS sweeper run back to back. The desktop calls it when its window comes
// forward or the user opens the device list, which is when a stale list is
// most visible and most annoying.
func (d *Discovery) Refresh() {
	d.mu.Lock()
	if d.stopping {
		d.mu.Unlock()
		return
	}
	d.bumpActiveLocked()
	broadcast := d.broadcast
	d.mu.Unlock()
	if broadcast != nil {
		broadcast.bump()
	}
	d.kick()
	// Wake the mDNS query loop immediately
	select {
	case d.queryWake <- struct{}{}:
	default:
	}
}

func (d *Discovery) bumpActiveLocked() {
	deadline := time.Now().Add(activeWindow)
	if deadline.After(d.activeUntil) {
		d.activeUntil = deadline
	}
}

func (d *Discovery) isActive() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return time.Now().Before(d.activeUntil)
}

// kick asks the probe loop for one extra round without waiting out its timer.
func (d *Discovery) kick() {
	select {
	case d.wake <- struct{}{}:
	default:
	}
}

// nextProbeDelay shortens the cadence inside the active window, but never
// beyond the configured interval — tests drive this far faster than any
// production setting and must not be slowed down by the hot path.
func (d *Discovery) nextProbeDelay() time.Duration {
	if !d.isActive() {
		return d.interval
	}
	if activeProbeInterval < d.interval {
		return activeProbeInterval
	}
	return d.interval
}

// queryLoop keeps asking the network who is out there. See mdns_query.go for
// why the zeroconf browser cannot be trusted to do this on its own.
func (d *Discovery) queryLoop() {
	for {
		querySweep(d.ctx, d.handleEntry)
		if d.ctx.Err() != nil {
			return
		}
		var wait time.Duration
		if d.isActive() {
			// Active period: sweep more frequently, but not back-to-back
			// to avoid excessive socket churn and mDNS traffic.
			wait = 500 * time.Millisecond
		} else {
			wait = idleQueryInterval
		}
		timer := time.NewTimer(wait)
		select {
		case <-d.ctx.Done():
			timer.Stop()
			return
		case <-d.queryWake:
			timer.Stop()
			// Refresh triggered - run another sweep immediately
		case <-timer.C:
		}
	}
}

// netmonLoop watches for the local addressing changing under us — a Wi-Fi
// handover, a VPN coming up, an ethernet cable going in. macOS gives no
// portable event for this, and polling the interface table is cheap enough
// that an event API would buy only tens of milliseconds.
func (d *Discovery) netmonLoop() {
	ticker := time.NewTicker(netPollInterval)
	defer ticker.Stop()
	previous := localNetworks()
	fingerprint := networksFingerprint(previous)
	for {
		select {
		case <-d.ctx.Done():
			return
		case <-ticker.C:
		}
		current := localNetworks()
		next := networksFingerprint(current)
		if next == fingerprint {
			continue
		}
		lost := subtractNets(previous, current)
		previous, fingerprint = current, next
		d.onNetworkChange(current, lost)
	}
}

// onNetworkChange reacts to new local addressing. Peers that lived only on a
// subnet we just left are unreachable by definition and go immediately;
// everything else — Tailscale, routed subnets — survives to be re-probed at
// the fast cadence rather than being guessed away.
func (d *Discovery) onNetworkChange(current, lost []*net.IPNet) {
	d.mu.Lock()
	if d.stopping {
		d.mu.Unlock()
		return
	}
	d.localNets = current
	removed := 0
	for key, peer := range d.peers {
		if peerStrandedBy(peer, lost) {
			delete(d.peers, key)
			delete(d.strike, key)
			removed++
		}
	}
	d.bumpActiveLocked()
	d.mu.Unlock()
	if removed > 0 {
		d.emitPeers()
	}
	if d.mdns != nil {
		if err := d.mdns.reannounce(d.cfg, LocalIPv4s()); err != nil {
			d.status("mDNS 重新宣告失败: " + err.Error())
		}
	}
	if d.broadcast != nil {
		d.broadcast.bump()
	}
	d.kick()
	// Wake mDNS query immediately after network change
	select {
	case d.queryWake <- struct{}{}:
	default:
	}
	d.status("网络已切换，正在重新发现设备")
}

// probeLoop is the authority on peer removal: it reprobes every peer in
// parallel and evicts on the strike policy probeRound applies.
func (d *Discovery) probeLoop() {
	for {
		timer := time.NewTimer(d.nextProbeDelay())
		select {
		case <-d.ctx.Done():
			timer.Stop()
			return
		case <-d.wake:
			timer.Stop()
		case <-timer.C:
		}
		d.probeRound()
	}
}

func (d *Discovery) probeRound() {
	d.mu.Lock()
	nets := append([]*net.IPNet(nil), d.localNets...)
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
		return
	}
	type outcome struct {
		key     string
		result  *ProbeResult
		conn    string
		verdict hostVerdict
	}
	results := make(chan outcome, len(targets))
	for _, item := range targets {
		go func(item target) {
			hosts := dedupeStrings(append([]string{item.peer.Host},
				item.peer.Hosts...))
			result, conn, verdict := d.probePeerHosts(hosts, item.peer.Port,
				item.peer.InstanceID, nets)
			results <- outcome{key: item.key, result: result, conn: conn,
				verdict: verdict}
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
		case out.verdict == hostMismatch:
			delete(d.peers, out.key)
			delete(d.strike, out.key)
			changed = true
			if !d.reported[out.key] {
				d.reported[out.key] = true
				d.mu.Unlock()
				d.status(peer.Name + " 身份验证失败")
				d.mu.Lock()
			}
		case out.verdict == hostAlive && out.result != nil:
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
		case out.verdict == hostGone:
			// Every address actively refused us. This is knowledge rather
			// than a timeout guess, so the dozing-phone tolerance does not
			// apply. A network change already removed peers stranded on its
			// departed subnet in onNetworkChange.
			delete(d.peers, out.key)
			delete(d.strike, out.key)
			changed = true
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

// hostVerdict summarizes what a peer's addresses told us.
type hostVerdict int

const (
	// hostSilent is the ambiguous case — a timeout, which a sleeping phone
	// produces just as readily as a departed one — and keeps the tolerant
	// strike count. It is the zero value so an unset verdict never evicts.
	hostSilent hostVerdict = iota
	hostAlive
	hostGone
	hostMismatch
)

// probePeerHosts races every known address of one peer. Probing serially was
// the other half of the slow-discovery problem: a peer remembered under a
// stale address paid that address's full timeout before the live one was even
// attempted.
func (d *Discovery) probePeerHosts(hosts []string, port int,
	expectedInstanceID string, nets []*net.IPNet) (*ProbeResult, string,
	hostVerdict) {
	if len(hosts) == 0 {
		return nil, "", hostGone
	}

	// Limit concurrent probes per peer to prevent resource exhaustion from
	// malicious large address lists. Keep at most 8 addresses per peer.
	const maxHostsPerPeer = 8
	if len(hosts) > maxHostsPerPeer {
		hosts = hosts[:maxHostsPerPeer]
	}

	type attempt struct {
		result  *ProbeResult
		host    string
		verdict hostVerdict
	}
	results := make(chan attempt, len(hosts))
	for _, host := range hosts {
		go func(host string) {
			timeout := d.timeout
			if onLocalNet(host, nets) && lanProbeTimeout < timeout {
				timeout = lanProbeTimeout
			}
			result, err := ProbePeer(host, port, timeout, expectedInstanceID)
			switch {
			case err == nil:
				connected := result.ConnectedIP
				if connected == "" {
					connected = host
				}
				results <- attempt{result: result, host: connected,
					verdict: hostAlive}
			case errors.Is(err, ErrIdentityMismatch):
				results <- attempt{verdict: hostMismatch}
			case isDefiniteRefusal(err):
				results <- attempt{verdict: hostGone}
			default:
				results <- attempt{verdict: hostSilent}
			}
		}(host)
	}
	// hostGone only survives if every address agrees; one silent address is
	// enough to fall back to the tolerant path.
	worst := hostGone
	for range hosts {
		select {
		case <-d.ctx.Done():
			return nil, "", hostSilent
		case item := <-results:
			switch item.verdict {
			case hostAlive:
				return item.result, item.host, hostAlive
			case hostMismatch:
				return nil, "", hostMismatch
			case hostSilent:
				worst = hostSilent
			}
		}
	}
	return nil, "", worst
}

// localNetworks snapshots the IPv4 networks this host currently sits on.
func localNetworks() []*net.IPNet {
	interfaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []*net.IPNet
	seen := make(map[string]bool)
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if iface.Flags&net.FlagPointToPoint != 0 ||
			!isLANInterfaceName(iface.Name) {
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
			masked := ip.Mask(ipNet.Mask)
			if masked == nil {
				continue
			}
			network := &net.IPNet{IP: masked, Mask: ipNet.Mask}
			key := network.String()
			if !seen[key] {
				seen[key] = true
				out = append(out, network)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].String() < out[j].String() })
	return out
}

func networksFingerprint(nets []*net.IPNet) string {
	// Include both network CIDRs and actual IPs to detect same-subnet changes
	parts := make([]string, 0, len(nets)*2)
	for _, network := range nets {
		parts = append(parts, network.String())
	}
	// Add local IPs
	ips := LocalIPv4s()
	for _, ip := range ips {
		parts = append(parts, "ip:"+ip)
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

// subtractNets returns the networks present in before but gone from after.
func subtractNets(before, after []*net.IPNet) []*net.IPNet {
	keep := make(map[string]bool, len(after))
	for _, network := range after {
		keep[network.String()] = true
	}
	var out []*net.IPNet
	for _, network := range before {
		if !keep[network.String()] {
			out = append(out, network)
		}
	}
	return out
}

func onLocalNet(host string, nets []*net.IPNet) bool {
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	for _, network := range nets {
		if network != nil && network.Contains(ip) {
			return true
		}
	}
	return false
}

// peerStrandedBy reports whether every address of a peer sat on a subnet we
// just lost, which makes it unreachable without probing anything.
func peerStrandedBy(peer *Peer, lost []*net.IPNet) bool {
	if len(lost) == 0 {
		return false
	}
	addresses := dedupeStrings(append([]string{peer.Host}, peer.Hosts...))
	if len(addresses) == 0 {
		return false
	}
	for _, host := range addresses {
		if !onLocalNet(host, lost) {
			return false
		}
	}
	return true
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

func stringInSlice(want string, values []string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

// isLANInterfaceName rejects tunnel, overlay and VM-only interfaces. Those
// paths remain available through manual/cross-network transports, but must not
// make a peer look local and receive the 700ms LAN probe budget.
func isLANInterfaceName(name string) bool {
	normalized := strings.ToLower(strings.TrimSpace(name))
	if normalized == "" {
		return false
	}
	for _, prefix := range []string{
		"utun", "tun", "tap", "wg", "tailscale", "zerotier", "zt",
		"ppp", "ipsec", "gif", "stf", "docker", "veth", "virbr", "vmnet",
		"vmware", "vethernet", "virtualbox", "vbox", "hyper-v",
	} {
		if strings.HasPrefix(normalized, prefix) {
			return false
		}
	}
	return true
}

// LocalIPv4s enumerates announceable IPv4 addresses: non-loopback,
// non-link-local and attached to a physical LAN-capable interface. Loopback
// never enters the list (see the 127.0.0.1 announcement bug).
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
		if iface.Flags&net.FlagPointToPoint != 0 ||
			!isLANInterfaceName(iface.Name) {
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
	sort.Strings(out)
	return out
}
