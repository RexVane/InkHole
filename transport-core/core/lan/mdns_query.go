package lan

import (
	"context"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/miekg/dns"
)

// Active mDNS querying, driven by us instead of by the zeroconf browser.
//
// grandcat/zeroconf v1.0.0 gives up querying permanently the moment its browse
// channel yields one entry (client.go:298 calls disableProbing), and this node
// always hears its own registration within milliseconds of starting. From then
// on the browser is purely passive: it reports unsolicited announcements and
// nothing else, so a phone that joins the network later stays invisible until
// it happens to re-announce on its own. The library's exponential backoff would
// abandon the query loop anyway after the backoff package's default 15-minute
// MaxElapsedTime. That is the whole reason this desktop found phones far more
// slowly than phones found it — JmDNS on the Android side keeps re-querying.
//
// So we send the questions ourselves. Each sweep asks for the service PTR from
// an ephemeral source port, which RFC 6762 §6.7 classifies as a legacy query:
// responders unicast the complete answer set straight back to that port rather
// than multicasting it. We therefore read and decode replies on our own socket,
// with no need to bind port 5353 and no chance of stealing packets from the
// system responder.

const (
	mdnsMulticastIPv4 = "224.0.0.251"
	mdnsPort          = 5353
	// qClassUnicastResponse is the top bit of the question's class field,
	// which RFC 6762 §5.4 defines as "please answer me directly".
	qClassUnicastResponse = 1 << 15
	// mdnsSweepWindow is how long one sweep listens for answers. RFC 6762 §6.3
	// lets responders defer shared-record replies by 20-120ms, and phones under
	// load take longer still, so a shorter window would drop real devices.
	mdnsSweepWindow = 900 * time.Millisecond
	// mdnsMaxPacket accommodates a full PTR+SRV+TXT+A answer set.
	mdnsMaxPacket = 9000
)

// querySweep asks every local interface for the service PTR in parallel and
// feeds each decoded answer to onEntry. It returns once every socket's listen
// window has closed, so a caller can use it as its own pacing mechanism.
func querySweep(ctx context.Context, onEntry func(mdnsEntry)) {
	question := new(dns.Msg)
	question.SetQuestion(mdnsService+"."+mdnsDomain, dns.TypePTR)
	question.RecursionDesired = false
	// Ask for a unicast reply. Apple's responder and JmDNS would already
	// answer an ephemeral-port query directly under the legacy rule of RFC
	// 6762 §5.4, but grandcat/zeroconf — what the other desktops run — only
	// looks at this bit and would otherwise multicast the answer somewhere
	// this socket never sees it.
	question.Question[0].Qclass |= qClassUnicastResponse
	payload, err := question.Pack()
	if err != nil {
		return
	}
	sources := LocalIPv4s()
	if len(sources) == 0 {
		// No usable address yet; still try the default route so a node on a
		// network we failed to enumerate is not invisible.
		sources = []string{""}
	}
	var wg sync.WaitGroup
	for _, source := range sources {
		wg.Add(1)
		go func(source string) {
			defer wg.Done()
			sweepFrom(ctx, source, payload, onEntry)
		}(source)
	}
	wg.Wait()
}

// sweepFrom runs one question/answer window bound to a single local address.
func sweepFrom(ctx context.Context, source string, payload []byte,
	onEntry func(mdnsEntry)) {
	var local *net.UDPAddr
	if source != "" {
		ip := net.ParseIP(source)
		if ip == nil {
			return
		}
		local = &net.UDPAddr{IP: ip}
	}
	conn, err := net.ListenUDP("udp4", local)
	if err != nil {
		return
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()

	target := &net.UDPAddr{IP: net.ParseIP(mdnsMulticastIPv4), Port: mdnsPort}
	if _, err := conn.WriteToUDP(payload, target); err != nil {
		return
	}
	deadline := time.Now().Add(mdnsSweepWindow)
	_ = conn.SetReadDeadline(deadline)
	buf := make([]byte, mdnsMaxPacket)
	for ctx.Err() == nil {
		n, _, err := conn.ReadFromUDP(buf)
		if err != nil {
			return
		}
		message := new(dns.Msg)
		if err := message.Unpack(buf[:n]); err != nil {
			continue
		}
		for _, entry := range decodeDNSAnswer(message) {
			onEntry(entry)
		}
	}
}

// decodeDNSAnswer assembles PTR/SRV/TXT/A records into service entries. It
// accepts records from every section because responders scatter the address
// records into Extra and some put the PTR in Answer only.
func decodeDNSAnswer(message *dns.Msg) []mdnsEntry {
	service := mdnsService + "." + mdnsDomain
	type partial struct {
		port int
		host string
		text []string
	}
	services := make(map[string]*partial)
	addresses := make(map[string][]net.IP)
	get := func(name string) *partial {
		if existing, ok := services[name]; ok {
			return existing
		}
		created := &partial{}
		services[name] = created
		return created
	}
	records := make([]dns.RR, 0,
		len(message.Answer)+len(message.Ns)+len(message.Extra))
	records = append(records, message.Answer...)
	records = append(records, message.Ns...)
	records = append(records, message.Extra...)
	for _, record := range records {
		switch rr := record.(type) {
		case *dns.PTR:
			if !strings.EqualFold(rr.Hdr.Name, service) {
				continue
			}
			get(rr.Ptr)
		case *dns.SRV:
			if !strings.HasSuffix(strings.ToLower(rr.Hdr.Name), service) {
				continue
			}
			entry := get(rr.Hdr.Name)
			entry.port = int(rr.Port)
			entry.host = strings.ToLower(rr.Target)
		case *dns.TXT:
			if !strings.HasSuffix(strings.ToLower(rr.Hdr.Name), service) {
				continue
			}
			entry := get(rr.Hdr.Name)
			entry.text = append(entry.text, rr.Txt...)
		case *dns.A:
			key := strings.ToLower(rr.Hdr.Name)
			addresses[key] = append(addresses[key], rr.A)
		}
	}
	out := make([]mdnsEntry, 0, len(services))
	for name, item := range services {
		label := strings.TrimSuffix(name, "."+service)
		label = strings.TrimSuffix(label, ".")
		decoded := buildEntry(name, label, item.port, addresses[item.host],
			item.text)
		if decoded != nil {
			out = append(out, *decoded)
		}
	}
	return out
}
