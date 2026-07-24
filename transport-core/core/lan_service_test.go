package core

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func lanCall(t *testing.T, service *Service, method string, params map[string]any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"id": "t-" + method, "method": method, "params": params,
	})
	if err != nil {
		t.Fatal(err)
	}
	var response Response
	if err := json.Unmarshal([]byte(service.HandleJSON(string(raw))), &response); err != nil {
		t.Fatal(err)
	}
	if !response.OK {
		t.Fatalf("%s failed: %s", method, response.Error)
	}
	result, _ := response.Result.(map[string]any)
	return result
}

func waitEvent(t *testing.T, service *Service, name string,
	timeout time.Duration) map[string]any {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case event := <-service.Events():
			if event.Event == name {
				data, _ := event.Data.(map[string]any)
				return data
			}
		case <-deadline:
			t.Fatalf("event %s never arrived", name)
		}
	}
}

// TestLANServiceTransfer drives two full service instances over the same
// JSON-RPC surface the desktop sidecar and Android AAR use.
func TestLANServiceTransfer(t *testing.T) {
	sender := NewService()
	receiver := NewService()
	t.Cleanup(func() { _ = sender.Close(); _ = receiver.Close() })

	inbox := t.TempDir()
	recvStart := lanCall(t, receiver, "lan.start", map[string]any{
		"peer_name":    "RPC收端",
		"instance_id":  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"inbox":        inbox,
		"disable_mdns": true,
		"disable_udp":  true,
	})
	if recvStart["identity_private"] == "" {
		t.Fatal("generated identity was not returned")
	}
	port := int(recvStart["port"].(float64))

	sendStart := lanCall(t, sender, "lan.start", map[string]any{
		"peer_name":    "RPC发端",
		"instance_id":  "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"inbox":        t.TempDir(),
		"disable_mdns": true,
		"disable_udp":  true,
	})

	payload := []byte("rpc transfer payload — 中文内容")
	path := filepath.Join(t.TempDir(), "rpc样本.txt")
	if err := os.WriteFile(path, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	sendResult := lanCall(t, sender, "lan.send", map[string]any{
		"session_id": sendStart["session_id"],
		"path":       path,
		"host":       "127.0.0.1",
		"port":       port,
	})
	if sendResult["send_id"] == "" {
		t.Fatal("send_id missing")
	}

	received := waitEvent(t, receiver, "lan.received", 10*time.Second)
	deliveredPath, _ := received["path"].(string)
	got, err := os.ReadFile(deliveredPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(payload) {
		t.Fatal("delivered payload mismatch")
	}
	sent := waitEvent(t, sender, "lan.sent", 10*time.Second)
	if ok, _ := sent["ok"].(bool); !ok {
		t.Fatalf("lan.sent reported failure: %v", sent["error"])
	}

	// Restarting with the returned identity must keep the fingerprint.
	restart := lanCall(t, receiver, "lan.start", map[string]any{
		"peer_name":        "RPC收端2",
		"instance_id":      "cccccccccccccccccccccccccccccccc",
		"inbox":            t.TempDir(),
		"identity_private": recvStart["identity_private"],
		"disable_mdns":     true,
		"disable_udp":      true,
	})
	if restart["fingerprint"] != recvStart["fingerprint"] {
		t.Fatal("identity fingerprint changed across restart")
	}
	lanCall(t, receiver, "lan.stop", map[string]any{
		"session_id": restart["session_id"]})
}
