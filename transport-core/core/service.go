package core

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"
)

type Service struct {
	ctx    context.Context
	cancel context.CancelFunc

	mu          sync.RWMutex
	localTarget string
	localToken  string
	deviceName  string
	instanceID  string
	sessions    map[string]session
	events      chan Event
	eventMu     sync.Mutex
	eventCond   *sync.Cond
	eventQueue  []Event
	eventWG     sync.WaitGroup
	// sshConnect is injectable for lifecycle tests; production uses the real
	// SSH reverse-forward connector.
	sshConnect sshReverseConnector
}

type session interface {
	Close() error
}

func NewService() *Service {
	ctx, cancel := context.WithCancel(context.Background())
	service := &Service{
		ctx:        ctx,
		cancel:     cancel,
		sessions:   make(map[string]session),
		events:     make(chan Event, 128),
		sshConnect: connectSSHReverse,
	}
	service.eventCond = sync.NewCond(&service.eventMu)
	service.eventWG.Add(1)
	go service.dispatchEvents()
	return service
}

func (s *Service) Close() error {
	s.cancel()
	s.eventMu.Lock()
	s.eventCond.Broadcast()
	s.eventMu.Unlock()
	s.mu.Lock()
	sessions := s.sessions
	s.sessions = make(map[string]session)
	s.mu.Unlock()
	for _, current := range sessions {
		_ = current.Close()
	}
	s.eventWG.Wait()
	return nil
}

func (s *Service) Events() <-chan Event { return s.events }

// droppableEvent lists the high-frequency snapshot events a slow consumer
// may lose without harm: progress repeats every chunk, status lines are
// advisory, and lan.peers always carries the full verified list again on
// the next change. Everything else (completion receipts, wormhole/ssh
// lifecycle) fires once and must be delivered.
func droppableEvent(name string) bool {
	switch name {
	case "lan.progress", "lan.status", "lan.peers":
		return true
	}
	return false
}

func (s *Service) emit(name string, data any) {
	event := Event{Event: name, Data: data}
	s.eventMu.Lock()
	defer s.eventMu.Unlock()
	if s.ctx.Err() != nil {
		return
	}
	if droppableEvent(name) && len(s.eventQueue) >= cap(s.events) {
		// Bound queued snapshots while a client is not consuming them. The
		// next update supersedes the dropped one.
		return
	}
	s.eventQueue = append(s.eventQueue, event)
	s.eventCond.Signal()
}

// dispatchEvents decouples transfer/session goroutines from UI event
// consumption. Snapshot traffic is bounded by emit; one-shot events stay in
// this ordered queue until the client consumes them or the service closes.
func (s *Service) dispatchEvents() {
	defer s.eventWG.Done()
	for {
		s.eventMu.Lock()
		for len(s.eventQueue) == 0 && s.ctx.Err() == nil {
			s.eventCond.Wait()
		}
		if s.ctx.Err() != nil {
			s.eventMu.Unlock()
			return
		}
		event := s.eventQueue[0]
		if len(s.eventQueue) == 1 {
			s.eventQueue = nil
		} else {
			s.eventQueue[0] = Event{}
			s.eventQueue = s.eventQueue[1:]
		}
		s.eventMu.Unlock()

		select {
		case s.events <- event:
		case <-s.ctx.Done():
			return
		}
	}
}

func (s *Service) HandleJSON(line string) string {
	var request Request
	if err := json.Unmarshal([]byte(line), &request); err != nil {
		return encodeResponse(Response{OK: false, Error: "invalid request: " + err.Error()})
	}
	if request.ID == "" || request.Method == "" {
		return encodeResponse(Response{ID: request.ID, OK: false, Error: "id and method are required"})
	}

	result, err := s.handle(request.Method, request.Params)
	if err != nil {
		return encodeResponse(Response{ID: request.ID, OK: false, Error: err.Error()})
	}
	return encodeResponse(Response{ID: request.ID, OK: true, Result: result})
}

func (s *Service) handle(method string, raw json.RawMessage) (any, error) {
	switch method {
	case "ping":
		return map[string]any{"protocol": ProtocolVersion}, nil
	case "start":
		var params StartParams
		if err := decodeParams(raw, &params); err != nil {
			return nil, err
		}
		if params.LocalTarget == "" || params.LocalToken == "" ||
			params.DeviceName == "" || params.InstanceID == "" {
			return nil, errors.New("local_target, local_token, device_name and instance_id are required")
		}
		s.mu.Lock()
		s.localTarget = params.LocalTarget
		s.localToken = params.LocalToken
		s.deviceName = params.DeviceName
		s.instanceID = params.InstanceID
		s.mu.Unlock()
		return map[string]any{"protocol": ProtocolVersion}, nil
	case "session.cancel":
		var params struct {
			SessionID string `json:"session_id"`
		}
		if err := decodeParams(raw, &params); err != nil {
			return nil, err
		}
		return map[string]any{"cancelled": s.removeSession(params.SessionID)}, nil
	case "wormhole.create":
		return s.createWormhole(raw)
	case "wormhole.join":
		return s.joinWormhole(raw)
	case "wormhole.join.start":
		return s.startJoinWormhole(raw)
	case "wormhole.accept":
		return s.acceptWormhole(raw)
	case "wormhole.reject":
		return s.rejectWormhole(raw)
	case "ssh.check":
		return s.checkSSH(raw)
	case "ssh.listen":
		return s.listenSSH(raw)
	case "ssh.pair.create":
		return s.createSSHPairing(raw)
	case "ssh.pair.join":
		return s.joinSSHPairing(raw)
	case "lan.start":
		return s.startLAN(raw)
	case "lan.stop":
		var params struct {
			SessionID string `json:"session_id"`
		}
		if err := decodeParams(raw, &params); err != nil {
			return nil, err
		}
		return map[string]any{"stopped": s.removeSession(params.SessionID)}, nil
	case "lan.peers":
		return s.lanPeers(raw)
	case "lan.send":
		return s.lanSend(raw)
	case "lan.send.cancel":
		return s.lanSendCancel(raw)
	default:
		return nil, errors.New("unknown method: " + method)
	}
}

func (s *Service) putSession(id string, current session) {
	s.mu.Lock()
	old := s.sessions[id]
	s.sessions[id] = current
	s.mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
}

func (s *Service) getSession(id string) session {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.sessions[id]
}

func (s *Service) removeSession(id string) bool {
	s.mu.Lock()
	current := s.sessions[id]
	if current == nil {
		s.mu.Unlock()
		return false
	}
	delete(s.sessions, id)
	s.mu.Unlock()
	_ = current.Close()
	return true
}

func (s *Service) target() (string, string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.localTarget == "" {
		return "", "", errors.New("transport core is not started")
	}
	return s.localTarget, s.localToken, nil
}

func sessionContext(parent context.Context, minutes int) (context.Context, context.CancelFunc) {
	if minutes <= 0 || minutes > 60 {
		minutes = 10
	}
	return context.WithTimeout(parent, time.Duration(minutes)*time.Minute)
}
