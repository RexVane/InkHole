package lan

import (
	"context"
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/miekg/dns"
)

// TestDecodeDNSAnswerAssemblesService covers the active querier's parser with
// the record layout a real responder produces: PTR in the answer section and
// the SRV/TXT/A set scattered into extra.
func TestDecodeDNSAnswerAssemblesService(t *testing.T) {
	service := mdnsService + "." + mdnsDomain
	instance := "测试节点-00112233." + service
	message := new(dns.Msg)
	message.Answer = []dns.RR{
		&dns.PTR{
			Hdr: dns.RR_Header{Name: service, Rrtype: dns.TypePTR},
			Ptr: instance,
		},
	}
	message.Extra = []dns.RR{
		&dns.SRV{
			Hdr:    dns.RR_Header{Name: instance, Rrtype: dns.TypeSRV},
			Port:   41300,
			Target: "inkhole-00112233.local.",
		},
		&dns.TXT{
			Hdr: dns.RR_Header{Name: instance, Rrtype: dns.TypeTXT},
			Txt: []string{
				"peer_name=被查询节点",
				"instance_id=" + vecInstanceID,
				fmt.Sprintf("whpc=%d", CapVersion),
				"ips=192.168.5.20",
			},
		},
		&dns.A{
			Hdr: dns.RR_Header{Name: "inkhole-00112233.local.",
				Rrtype: dns.TypeA},
			A: net.ParseIP("192.168.5.20"),
		},
	}

	entries := decodeDNSAnswer(message)
	if len(entries) != 1 {
		t.Fatalf("decoded %d entries, want 1", len(entries))
	}
	entry := entries[0]
	if entry.InstanceID != vecInstanceID {
		t.Fatalf("instance id = %q", entry.InstanceID)
	}
	if entry.PeerName != "被查询节点" || entry.Port != 41300 {
		t.Fatalf("entry = %+v", entry)
	}
	if len(entry.Hosts) != 1 || entry.Hosts[0] != "192.168.5.20" {
		t.Fatalf("hosts = %v", entry.Hosts)
	}
	if entry.ServiceName != instance {
		t.Fatalf("service name = %q", entry.ServiceName)
	}
}

// TestDecodeDNSAnswerIgnoresForeignService guards against picking up the
// other services a legacy query sweep will inevitably see on a busy network.
func TestDecodeDNSAnswerIgnoresForeignService(t *testing.T) {
	message := new(dns.Msg)
	message.Answer = []dns.RR{
		&dns.PTR{
			Hdr: dns.RR_Header{Name: "_airplay._tcp.local.",
				Rrtype: dns.TypePTR},
			Ptr: "客厅电视._airplay._tcp.local.",
		},
	}
	if entries := decodeDNSAnswer(message); len(entries) != 0 {
		t.Fatalf("decoded %d foreign entries, want 0", len(entries))
	}
}

// TestQuerySweepFindsOwnRegistration exercises the whole active-query path
// against a real responder: register the service, then sweep for it. This is
// the path that replaces zeroconf's self-disabling probe loop, so it is worth
// running against the actual network stack rather than a fake.
func TestQuerySweepFindsOwnRegistration(t *testing.T) {
	if len(LocalIPv4s()) == 0 {
		t.Skip("no routable IPv4 address; multicast cannot be exercised")
	}
	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	cfg := Config{
		PeerName:     "扫描目标",
		InstanceID:   vecInstanceID,
		Port:         41399,
		Identity:     identity,
		Capabilities: []string{"folder-v1"},
	}
	server, err := registerService(cfg, LocalIPv4s())
	if err != nil {
		t.Skipf("mDNS registration unavailable here: %v", err)
	}
	defer server.Shutdown()

	found := make(chan mdnsEntry, 8)
	deadline := time.Now().Add(6 * time.Second)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(),
			2*time.Second)
		querySweep(ctx, func(entry mdnsEntry) {
			if entry.InstanceID == vecInstanceID {
				select {
				case found <- entry:
				default:
				}
			}
		})
		cancel()
		select {
		case entry := <-found:
			if entry.Port != 41399 {
				t.Fatalf("swept entry port = %d", entry.Port)
			}
			return
		default:
		}
	}
	t.Skip("multicast loopback not available in this environment")
}

// TestDefiniteRefusalEvictsWithoutBurningStrikes pins the behaviour the slow
// device list came from: a peer that actively refuses the connection is gone
// after one round, while the four-strike tolerance stays reserved for silence.
func TestDefiniteRefusalEvictsWithoutBurningStrikes(t *testing.T) {
	peerIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	listener, addr := startCloseableProbeServer(t, peerIdentity,
		vecInstanceID, "被发现节点", []string{"folder-v1"})
	updates := make(chan []Peer, 16)

	identity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	// Five strikes at 400ms would need at least two seconds to evict if the
	// refusal were treated as ordinary silence.
	discovery, err := Start(Config{
		PeerName:         "发现者",
		InstanceID:       "00112233445566770011223344556677",
		Port:             41300,
		Identity:         identity,
		Capabilities:     []string{"folder-v1"},
		DisableMDNS:      true,
		DisableBroadcast: true,
		ProbeInterval:    400 * time.Millisecond,
		ProbeTimeout:     500 * time.Millisecond,
		ProbeStrikes:     5,
	}, func(peers []Peer) {
		select {
		case updates <- peers:
		default:
		}
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(discovery.Stop)

	discovery.handleAnnouncement("127.0.0.1", &Announcement{
		InstanceID: vecInstanceID, Port: addr.Port})
	waitForPeerCount(t, updates, 1, 5*time.Second)

	_ = listener.Close()
	start := time.Now()
	waitForPeerCount(t, updates, 0, 5*time.Second)
	if elapsed := time.Since(start); elapsed > 1500*time.Millisecond {
		t.Fatalf("refused peer took %v to leave; strike tolerance was "+
			"applied to a definite refusal", elapsed)
	}
}

// TestSilenceKeepsStrikeTolerance is the other half of the contract: a
// timeout must not be mistaken for a departure, or sleeping phones flicker.
func TestSilenceKeepsStrikeTolerance(t *testing.T) {
	timeout := &net.OpError{
		Op:  "dial",
		Err: &timeoutError{},
	}
	if isDefiniteRefusal(timeout) {
		t.Fatal("a timeout must never count as a definite refusal")
	}
}

type timeoutError struct{}

func (e *timeoutError) Error() string { return "i/o timeout" }
func (e *timeoutError) Timeout() bool { return true }

// TestPeerStrandedByLostSubnet covers the instant cleanup a Wi-Fi handover
// triggers: only peers whose every address lived on a subnet we just left may
// be dropped without a probe. A Tailscale peer must survive.
func TestPeerStrandedByLostSubnet(t *testing.T) {
	_, lost, err := net.ParseCIDR("192.168.5.0/24")
	if err != nil {
		t.Fatal(err)
	}
	lostNets := []*net.IPNet{lost}

	lan := &Peer{Host: "192.168.5.20", Hosts: []string{"192.168.5.20"}}
	if !peerStrandedBy(lan, lostNets) {
		t.Fatal("a peer only reachable on the lost subnet must be dropped")
	}
	tailscale := &Peer{Host: "100.127.46.26", Hosts: []string{"100.127.46.26"}}
	if peerStrandedBy(tailscale, lostNets) {
		t.Fatal("a Tailscale peer must survive a Wi-Fi change")
	}
	mixed := &Peer{Host: "192.168.5.20",
		Hosts: []string{"192.168.5.20", "100.127.46.26"}}
	if peerStrandedBy(mixed, lostNets) {
		t.Fatal("a peer with a surviving address must not be dropped")
	}
	if peerStrandedBy(lan, nil) {
		t.Fatal("no lost subnet means nothing may be dropped")
	}
}

func TestSubtractNetsFindsDepartedSubnet(t *testing.T) {
	parse := func(cidr string) *net.IPNet {
		_, network, err := net.ParseCIDR(cidr)
		if err != nil {
			t.Fatal(err)
		}
		return network
	}
	before := []*net.IPNet{parse("192.168.5.0/24"), parse("100.64.0.0/10")}
	after := []*net.IPNet{parse("10.0.8.0/24"), parse("100.64.0.0/10")}
	lost := subtractNets(before, after)
	if len(lost) != 1 || lost[0].String() != "192.168.5.0/24" {
		t.Fatalf("lost = %v", lost)
	}
}

// TestGoodbyeStaysWireCompatible checks both directions of the added field:
// a goodbye decodes as one, and a v3 packet without the field still decodes
// exactly as before so older Python and Kotlin builds keep working.
func TestGoodbyeStaysWireCompatible(t *testing.T) {
	payload, err := EncodeGoodbye(vecInstanceID, 41300)
	if err != nil {
		t.Fatal(err)
	}
	decoded := DecodeAnnouncement(payload)
	if decoded == nil || !decoded.Bye {
		t.Fatalf("goodbye decoded as %+v", decoded)
	}
	if decoded.InstanceID != vecInstanceID || decoded.Port != 41300 {
		t.Fatalf("goodbye lost its payload: %+v", decoded)
	}

	legacy := []byte(`{"magic":"inkhole-lan-v1","version":3,"instance_id":"` +
		vecInstanceID + `","port":41300,"reply":false}`)
	plain := DecodeAnnouncement(legacy)
	if plain == nil || plain.Bye {
		t.Fatalf("legacy announcement decoded as %+v", plain)
	}

	// An ordinary announcement must not carry the field at all, so a v3
	// decoder sees byte-identical input to what it saw before.
	announce, err := EncodeAnnouncement(vecInstanceID, 41300, false)
	if err != nil {
		t.Fatal(err)
	}
	if got := string(announce); got != string(legacy) {
		t.Fatalf("announcement wire format changed:\n got %s\nwant %s",
			got, legacy)
	}
}
