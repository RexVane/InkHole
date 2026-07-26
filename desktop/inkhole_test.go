package main

import (
	"net"
	"strings"
	"testing"
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
