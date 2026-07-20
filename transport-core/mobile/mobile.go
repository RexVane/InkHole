// Package mobile provides a gomobile-friendly facade over the shared service.
package mobile

import (
	"sync"
	"time"

	"github.com/rexvane/inkhole/transport-core/core"
)

var (
	serviceMu sync.Mutex
	service   = core.NewService()
)

// Call dispatches one JSON request and returns one JSON response.
func Call(request string) string {
	serviceMu.Lock()
	current := service
	serviceMu.Unlock()
	return current.HandleJSON(request)
}

// Poll waits for an asynchronous event. An empty string means timeout.
func Poll(timeoutMillis int64) string {
	serviceMu.Lock()
	current := service
	serviceMu.Unlock()
	if timeoutMillis < 0 {
		timeoutMillis = 0
	}
	if timeoutMillis > 60_000 {
		timeoutMillis = 60_000
	}
	select {
	case event := <-current.Events():
		return core.EncodeEvent(event)
	case <-time.After(time.Duration(timeoutMillis) * time.Millisecond):
		return ""
	}
}

// Reset closes all active sessions and creates a clean service instance.
func Reset() {
	serviceMu.Lock()
	defer serviceMu.Unlock()
	_ = service.Close()
	service = core.NewService()
}
