package main

import (
	"net"
	"strings"
	"testing"
	"time"

	"github.com/rexvane/inkhole/transport-core/core/lan"
)

func TestManualPeerNeedsFourFailedProbesBeforeRemoval(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()

	instanceID := strings.Repeat("d", 32)
	previous := PeerView{InstanceID: instanceID, Name: "休眠设备",
		Host: "127.0.0.1", Port: port, Transport: "Tailscale/固定地址"}
	service := NewInkHoleService()
	service.running = true
	service.cfg.ManualPeers = []ManualPeerConfig{{Name: previous.Name,
		Host: previous.Host, Port: previous.Port, InstanceID: instanceID}}
	service.manualRuntime[instanceID] = previous

	for round := 1; round <= manualProbeStrikes; round++ {
		service.probeManualPeers()
		_, online := service.manualRuntime[instanceID]
		if round < manualProbeStrikes && !online {
			t.Fatalf("manual peer disappeared after only %d failed probes", round)
		}
		if round == manualProbeStrikes && online {
			t.Fatalf("manual peer remained after %d failed probes", round)
		}
	}
}

func TestManualProbeResultsFollowEndpointAfterReorder(t *testing.T) {
	service := NewInkHoleService()
	service.running = true
	service.cfg.ManualPeers = []ManualPeerConfig{
		{Name: "A", Host: "a.example", Port: 1001},
		{Name: "B", Host: "b.example", Port: 1002},
	}
	started := make(chan struct{}, 2)
	release := make(chan struct{})
	service.manualProbe = func(host string, _ int, _ time.Duration,
		_ string) (*lan.ProbeResult, error) {
		started <- struct{}{}
		<-release
		if host == "a.example" {
			return &lan.ProbeResult{InstanceID: strings.Repeat("a", 32),
				PeerName: "A", Fingerprint: "fingerprint-a"}, nil
		}
		return &lan.ProbeResult{InstanceID: strings.Repeat("b", 32),
			PeerName: "B", Fingerprint: "fingerprint-b"}, nil
	}
	done := make(chan struct{})
	go func() {
		service.probeManualPeers()
		close(done)
	}()
	<-started
	<-started
	service.mu.Lock()
	service.cfg.ManualPeers[0], service.cfg.ManualPeers[1] =
		service.cfg.ManualPeers[1], service.cfg.ManualPeers[0]
	service.mu.Unlock()
	close(release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("manual probe did not finish")
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.cfg.ManualPeers[0].Host != "b.example" ||
		service.cfg.ManualPeers[0].InstanceID != strings.Repeat("b", 32) ||
		service.cfg.ManualPeers[1].Host != "a.example" ||
		service.cfg.ManualPeers[1].InstanceID != strings.Repeat("a", 32) {
		t.Fatalf("probe results were written by stale index: %+v", service.cfg.ManualPeers)
	}
}

func TestSavingManualPeersInvalidatesInFlightProbe(t *testing.T) {
	service := NewInkHoleService()
	service.running = true
	service.cfg.ManualPeers = []ManualPeerConfig{{Host: "old.example", Port: 1001}}
	started := make(chan struct{})
	release := make(chan struct{})
	service.manualProbe = func(string, int, time.Duration,
		string) (*lan.ProbeResult, error) {
		close(started)
		<-release
		return &lan.ProbeResult{InstanceID: strings.Repeat("a", 32),
			Fingerprint: "old-fingerprint"}, nil
	}
	done := make(chan struct{})
	go func() {
		service.probeManualPeers()
		close(done)
	}()
	<-started
	service.mu.Lock()
	service.manualProbeGen++
	service.cfg.ManualPeers = []ManualPeerConfig{{Host: "new.example", Port: 2002}}
	service.mu.Unlock()
	close(release)
	<-done
	if service.cfg.ManualPeers[0].InstanceID != "" {
		t.Fatal("stale probe modified newly saved manual peer")
	}
}
