package lan

import (
	"context"
	"fmt"
	"net"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/grandcat/zeroconf"
)

// mDNS registration/browsing for _inkhole._tcp.local., interoperable with
// python-zeroconf, Android NSD and JmDNS. Offline detection is NOT driven
// by mDNS goodbye packets (they are lost too often to be trusted; the git
// history of ghost-device fixes proves it) — the liveness prober owns peer
// removal, mDNS only feeds candidates.

const (
	mdnsService = "_inkhole._tcp"
	mdnsDomain  = "local."
)

// ServiceLabel mirrors p2p._service_label: dots swapped out of the display
// name (DNS label separators), utf-8 truncated to 40 bytes on a rune
// boundary, then a unique instance suffix.
func ServiceLabel(name, instanceID string) string {
	label := strings.ReplaceAll(name, ".", "-")
	raw := []byte(label)
	if len(raw) > 40 {
		raw = raw[:40]
		for len(raw) > 0 {
			r, size := utf8.DecodeLastRune(raw)
			if r == utf8.RuneError && size == 1 {
				raw = raw[:len(raw)-1]
				continue
			}
			break
		}
	}
	suffix := instanceID
	if len(suffix) > 8 {
		suffix = suffix[:8]
	}
	return fmt.Sprintf("%s-%s", string(raw), suffix)
}

// mdnsEntry is one browsed service, normalized from TXT records.
type mdnsEntry struct {
	ServiceName string
	InstanceID  string
	PeerName    string
	Port        int
	Hosts       []string
}

type mdnsLayer struct {
	mu     sync.Mutex
	server *zeroconf.Server
	cancel context.CancelFunc
	ips    []string
	wg     sync.WaitGroup
}

// registerService publishes this node under the current address set.
func registerService(cfg Config, localIPs []string) (*zeroconf.Server, error) {
	instance := ServiceLabel(cfg.PeerName, cfg.InstanceID)
	txt := []string{
		"peer_name=" + cfg.PeerName,
		"instance_id=" + cfg.InstanceID,
		fmt.Sprintf("whpc=%d", CapVersion),
		"caps=" + strings.Join(cfg.Capabilities, ","),
		"identity=" + cfg.Identity.Fingerprint,
		"ips=" + strings.Join(localIPs, ","),
	}
	host := "inkhole-" + cfg.InstanceID[:8]
	return zeroconf.RegisterProxy(instance, mdnsService, mdnsDomain,
		cfg.Port, host, localIPs, txt, nil)
}

// reannounce republishes the service under a new address set. mDNS carries
// addresses inside the records themselves, so after a Wi-Fi handover the old
// registration keeps advertising an IP nobody can reach — and the TXT "ips"
// list that Android NSD relies on would stay stale for the life of the
// process. Only re-registering fixes that.
func (m *mdnsLayer) reannounce(cfg Config, localIPs []string) error {
	if len(localIPs) == 0 {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if sameStrings(m.ips, localIPs) {
		return nil
	}
	// Register the replacement before retiring the current responder. If the
	// new interface is still settling and registration fails, the old service
	// remains alive and the next network event can retry instead of leaving
	// discovery permanently unpublished.
	server, err := registerService(cfg, localIPs)
	if err != nil {
		return err
	}
	previous := m.server
	m.server = server
	m.ips = append([]string(nil), localIPs...)
	if previous != nil {
		previous.Shutdown()
	}
	return nil
}

func sameStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func parseTXT(text []string) map[string]string {
	out := make(map[string]string, len(text))
	for _, item := range text {
		key, value, found := strings.Cut(item, "=")
		if found {
			out[key] = value
		}
	}
	return out
}

// startMDNS registers this node and browses for peers until ctx ends.
// Every plausible peer entry is handed to onEntry for WHPC verification.
//
// The browser is only half the story: see mdns_query.go for why this package
// also drives its own queries instead of trusting zeroconf's probe loop.
func startMDNS(ctx context.Context, cfg Config, localIPs []string,
	onEntry func(mdnsEntry)) (*mdnsLayer, error) {
	server, err := registerService(cfg, localIPs)
	if err != nil {
		return nil, err
	}

	browseCtx, cancel := context.WithCancel(ctx)
	resolver, err := zeroconf.NewResolver(nil)
	if err != nil {
		server.Shutdown()
		cancel()
		return nil, err
	}
	entries := make(chan *zeroconf.ServiceEntry, 16)
	layer := &mdnsLayer{
		server: server,
		cancel: cancel,
		ips:    append([]string(nil), localIPs...),
	}
	layer.wg.Add(1)
	go func() {
		defer layer.wg.Done()
		for {
			select {
			case <-browseCtx.Done():
				return
			case entry, ok := <-entries:
				if !ok {
					return
				}
				if entry == nil {
					continue
				}
				decoded := decodeEntry(entry)
				if decoded == nil || decoded.InstanceID == cfg.InstanceID {
					continue
				}
				onEntry(*decoded)
			}
		}
	}()
	if err := resolver.Browse(browseCtx, mdnsService, mdnsDomain, entries); err != nil {
		cancel()
		layer.wg.Wait()
		server.Shutdown()
		return nil, err
	}
	return layer, nil
}

func decodeEntry(entry *zeroconf.ServiceEntry) *mdnsEntry {
	return buildEntry(entry.ServiceInstanceName(), entry.Instance, entry.Port,
		entry.AddrIPv4, entry.Text)
}

// buildEntry normalizes one browsed or queried service into an mdnsEntry.
// Both the zeroconf browser and the active querier in mdns_query.go funnel
// through here so the two paths cannot drift in what they accept.
func buildEntry(serviceInstance, label string, port int, addrs []net.IP,
	text []string) *mdnsEntry {
	txt := parseTXT(text)
	instanceID := strings.ToLower(txt["instance_id"])
	if !ValidInstanceID(instanceID) ||
		txt["whpc"] != fmt.Sprintf("%d", CapVersion) {
		return nil
	}
	if port < 1 || port > 65535 {
		return nil
	}
	seen := make(map[string]bool)
	var hosts []string
	push := func(raw string) {
		host := strings.TrimSpace(raw)
		if host == "" || seen[host] {
			return
		}
		if ip := net.ParseIP(host); ip == nil || ip.To4() == nil ||
			ip.IsLoopback() || ip.IsLinkLocalUnicast() {
			return
		}
		seen[host] = true
		hosts = append(hosts, host)
	}
	for _, ip := range addrs {
		push(ip.String())
	}
	// The txt "ips" list covers Android NSD, which resolves only one address.
	for _, raw := range strings.Split(txt["ips"], ",") {
		push(raw)
	}
	if len(hosts) == 0 {
		return nil
	}
	name := strings.TrimSpace(txt["peer_name"])
	if name == "" {
		name = strings.SplitN(label, ".", 2)[0]
	}
	return &mdnsEntry{
		ServiceName: serviceInstance,
		InstanceID:  instanceID,
		PeerName:    name,
		Port:        port,
		Hosts:       hosts,
	}
}

func (m *mdnsLayer) stop() {
	m.cancel()
	m.wg.Wait()
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.server != nil {
		m.server.Shutdown()
		m.server = nil
	}
}
