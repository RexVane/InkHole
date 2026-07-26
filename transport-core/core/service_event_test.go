package core

import (
	"testing"
	"time"
)

func fillEventOutput(t *testing.T, service *Service) {
	t.Helper()
	for i := 0; i < cap(service.events)*4; i++ {
		service.emit("lan.progress", i)
	}
	deadline := time.Now().Add(time.Second)
	for len(service.events) != cap(service.events) {
		if time.Now().After(deadline) {
			t.Fatalf("event output length = %d, want %d",
				len(service.events), cap(service.events))
		}
		time.Sleep(time.Millisecond)
	}
}

func TestCriticalEventQueuesWithoutBlockingAndIsDelivered(t *testing.T) {
	service := NewService()
	t.Cleanup(func() { _ = service.Close() })
	fillEventOutput(t, service)

	done := make(chan struct{})
	go func() {
		service.emit("wormhole.ready", map[string]any{"session_id": "critical"})
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("critical event blocked behind a full client queue")
	}

	deadline := time.After(2 * time.Second)
	for {
		select {
		case event := <-service.Events():
			if event.Event == "wormhole.ready" {
				return
			}
		case <-deadline:
			t.Fatal("critical event was not delivered after the client resumed")
		}
	}
}

func TestDroppableQueueIsBoundedAndCloseUnblocksDispatcher(t *testing.T) {
	service := NewService()
	fillEventOutput(t, service)

	for i := 0; i < cap(service.events)*4; i++ {
		service.emit("lan.status", "advisory")
	}
	service.eventMu.Lock()
	queued := len(service.eventQueue)
	service.eventMu.Unlock()
	if queued > cap(service.events) {
		t.Fatalf("droppable event queue grew to %d, limit %d",
			queued, cap(service.events))
	}

	service.emit("lan.sent", map[string]any{"ok": true})
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}
