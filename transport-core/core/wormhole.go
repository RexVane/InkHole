package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	william "github.com/psanford/wormhole-william/wormhole"
)

const wormholeAppID = "com.rexvane.inkhole/transport-v1"

type TransferSummary struct {
	DeviceName     string   `json:"device_name"`
	InstanceID     string   `json:"instance_id"`
	ItemCount      int      `json:"item_count"`
	FileCount      int      `json:"file_count"`
	DirectoryCount int      `json:"directory_count"`
	TotalBytes     int64    `json:"total_bytes"`
	Names          []string `json:"names,omitempty"`
}

type wormholeSettings struct {
	RendezvousURL string `json:"rendezvous_url,omitempty"`
	TransitRelay  string `json:"transit_relay,omitempty"`
	ProxyURL      string `json:"proxy_url,omitempty"`
	TimeoutMinute int    `json:"timeout_minutes,omitempty"`
}

type createWormholeParams struct {
	Summary  TransferSummary  `json:"summary"`
	Settings wormholeSettings `json:"settings,omitempty"`
}

type joinWormholeParams struct {
	Code     string           `json:"code"`
	Settings wormholeSettings `json:"settings,omitempty"`
}

type wormholeSession struct {
	mu       sync.Mutex
	ctx      context.Context
	cancel   context.CancelFunc
	offer    *william.TunnelOffer
	claimed  bool
	bridge   *streamBridge
	finished bool
}

func (w *wormholeSession) Close() error {
	w.mu.Lock()
	wasFinished := w.finished
	w.cancel()
	bridge := w.bridge
	offer := w.offer
	w.finished = true
	w.mu.Unlock()
	if bridge != nil {
		_ = bridge.Close()
	}
	if offer != nil && !wasFinished {
		_ = offer.Reject()
	}
	return nil
}

func (w *wormholeSession) setOffer(offer *william.TunnelOffer) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.finished || w.ctx.Err() != nil {
		return false
	}
	w.offer = offer
	w.claimed = false
	return true
}

func (w *wormholeSession) claimOffer() *william.TunnelOffer {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.finished || w.claimed || w.ctx.Err() != nil || w.offer == nil {
		return nil
	}
	w.claimed = true
	return w.offer
}

func (w *wormholeSession) currentOffer() *william.TunnelOffer {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.finished {
		return nil
	}
	return w.offer
}

func (w *wormholeSession) installBridge(bridge *streamBridge) bool {
	w.mu.Lock()
	if w.finished || w.ctx.Err() != nil {
		w.mu.Unlock()
		_ = bridge.Close()
		return false
	}
	w.bridge = bridge
	w.finished = true
	w.mu.Unlock()
	return true
}

func newWormholeClient(settings wormholeSettings) william.Client {
	proxyURL := strings.TrimSpace(settings.ProxyURL)
	if proxyURL == "" {
		proxyURL = strings.TrimSpace(os.Getenv("INKHOLE_PROXY_URL"))
	}
	return william.Client{
		AppID:                     wormholeAppID,
		RendezvousURL:             strings.TrimSpace(settings.RendezvousURL),
		TransitRelayAddress:       strings.TrimSpace(settings.TransitRelay),
		ProxyURL:                  proxyURL,
		PassPhraseComponentLength: 2,
	}
}

func validateSummary(summary *TransferSummary) error {
	if strings.TrimSpace(summary.DeviceName) == "" || strings.TrimSpace(summary.InstanceID) == "" {
		return errors.New("summary device_name and instance_id are required")
	}
	if summary.ItemCount <= 0 || summary.TotalBytes < 0 {
		return errors.New("summary item_count or total_bytes is invalid")
	}
	if summary.FileCount < 0 || summary.DirectoryCount < 0 ||
		summary.FileCount+summary.DirectoryCount < summary.ItemCount {
		return errors.New("summary item counts are invalid")
	}
	if len(summary.Names) > 8 {
		summary.Names = summary.Names[:8]
	}
	for index, name := range summary.Names {
		summary.Names[index] = strings.TrimSpace(name)
	}
	return nil
}

func (s *Service) createWormhole(raw json.RawMessage) (any, error) {
	var params createWormholeParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	if err := validateSummary(&params.Summary); err != nil {
		return nil, err
	}
	metadata, err := json.Marshal(params.Summary)
	if err != nil {
		return nil, err
	}

	ctx, cancel := sessionContext(s.ctx, params.Settings.TimeoutMinute)
	client := newWormholeClient(params.Settings)
	code, result, err := client.OpenTunnel(ctx, string(metadata))
	if err != nil {
		cancel()
		return nil, fmt.Errorf("create wormhole: %w", err)
	}
	id := randomID("wh")
	current := &wormholeSession{ctx: ctx, cancel: cancel}
	s.putSession(id, current)

	go func() {
		opened, ok := <-result
		if !ok {
			s.emit("wormhole.error", map[string]any{"session_id": id, "error": "short-code session ended"})
			s.removeSession(id)
			return
		}
		if opened.Error != nil {
			s.emit("wormhole.error", map[string]any{"session_id": id, "error": opened.Error.Error()})
			s.removeSession(id)
			return
		}
		bridge, err := newSendingBridge(current.ctx, opened.Conn)
		if err != nil {
			s.emit("wormhole.error", map[string]any{"session_id": id, "error": err.Error()})
			s.removeSession(id)
			return
		}
		if !current.installBridge(bridge) {
			s.removeSession(id)
			return
		}
		s.emit("wormhole.ready", map[string]any{
			"session_id":     id,
			"local_endpoint": bridge.Addr(),
			"endpoint_token": bridge.Token(),
			"role":           "sender",
		})
	}()

	expires := time.Now().Add(time.Duration(normalizeTimeout(params.Settings.TimeoutMinute)) * time.Minute)
	return map[string]any{
		"session_id": id,
		"code":       code,
		"uri":        "inkhole://receive?code=" + code,
		"expires_at": expires.UTC().Format(time.RFC3339),
	}, nil
}

func (s *Service) joinWormhole(raw json.RawMessage) (any, error) {
	var params joinWormholeParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	params.Code = strings.TrimSpace(params.Code)
	if params.Code == "" {
		return nil, errors.New("code is required")
	}
	ctx, cancel := sessionContext(s.ctx, params.Settings.TimeoutMinute)
	client := newWormholeClient(params.Settings)
	offer, err := client.ReceiveTunnel(ctx, params.Code)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("join wormhole: %w", err)
	}
	var summary TransferSummary
	if err := json.Unmarshal([]byte(offer.Metadata), &summary); err != nil {
		_ = offer.Reject()
		cancel()
		return nil, errors.New("sender returned invalid InkHole transfer summary")
	}
	if err := validateSummary(&summary); err != nil {
		_ = offer.Reject()
		cancel()
		return nil, err
	}
	id := randomID("wh")
	s.putSession(id, &wormholeSession{ctx: ctx, cancel: cancel, offer: offer})
	return map[string]any{"session_id": id, "summary": summary}, nil
}

// startJoinWormhole starts the receive rendezvous in the background so mobile
// callers can dismiss and cancel a pending code before a sender appears.
func (s *Service) startJoinWormhole(raw json.RawMessage) (any, error) {
	var params joinWormholeParams
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	params.Code = strings.TrimSpace(params.Code)
	if params.Code == "" {
		return nil, errors.New("code is required")
	}
	ctx, cancel := sessionContext(s.ctx, params.Settings.TimeoutMinute)
	id := randomID("wh")
	current := &wormholeSession{ctx: ctx, cancel: cancel}
	s.putSession(id, current)
	client := newWormholeClient(params.Settings)

	go func() {
		offer, err := client.ReceiveTunnel(ctx, params.Code)
		if err != nil {
			if ctx.Err() == nil || errors.Is(ctx.Err(), context.DeadlineExceeded) {
				s.emit("wormhole.error", map[string]any{
					"session_id": id, "error": fmt.Sprintf("join wormhole: %v", err)})
			}
			s.removeSession(id)
			return
		}
		var summary TransferSummary
		if err := json.Unmarshal([]byte(offer.Metadata), &summary); err != nil {
			_ = offer.Reject()
			s.emit("wormhole.error", map[string]any{
				"session_id": id, "error": "sender returned invalid InkHole transfer summary"})
			s.removeSession(id)
			return
		}
		if err := validateSummary(&summary); err != nil {
			_ = offer.Reject()
			s.emit("wormhole.error", map[string]any{
				"session_id": id, "error": err.Error()})
			s.removeSession(id)
			return
		}
		if !current.setOffer(offer) {
			_ = offer.Reject()
			s.removeSession(id)
			return
		}
		s.emit("wormhole.offer", map[string]any{"session_id": id, "summary": summary})
	}()

	return map[string]any{"session_id": id}, nil
}

func (s *Service) acceptWormhole(raw json.RawMessage) (any, error) {
	var params struct {
		SessionID string `json:"session_id"`
	}
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	current, ok := s.getSession(params.SessionID).(*wormholeSession)
	if !ok {
		return nil, errors.New("wormhole offer not found")
	}
	target, targetToken, err := s.target()
	if err != nil {
		return nil, err
	}
	offer := current.claimOffer()
	if offer == nil {
		return nil, errors.New("wormhole offer not found")
	}
	conn, err := offer.Accept()
	if err != nil {
		s.removeSession(params.SessionID)
		return nil, fmt.Errorf("accept wormhole: %w", err)
	}
	bridge, err := newReceivingBridge(current.ctx, conn, target, targetToken)
	if err != nil {
		s.removeSession(params.SessionID)
		return nil, err
	}
	if !current.installBridge(bridge) {
		s.removeSession(params.SessionID)
		return nil, errors.New("wormhole session was cancelled")
	}
	s.emit("wormhole.ready", map[string]any{
		"session_id": params.SessionID,
		"role":       "receiver",
	})
	return map[string]any{"accepted": true}, nil
}

func (s *Service) rejectWormhole(raw json.RawMessage) (any, error) {
	var params struct {
		SessionID string `json:"session_id"`
	}
	if err := decodeParams(raw, &params); err != nil {
		return nil, err
	}
	current, ok := s.getSession(params.SessionID).(*wormholeSession)
	if !ok {
		return nil, errors.New("wormhole offer not found")
	}
	offer := current.claimOffer()
	if offer == nil {
		return nil, errors.New("wormhole offer not found")
	}
	if err := offer.Reject(); err != nil {
		return nil, err
	}
	current.mu.Lock()
	current.finished = true
	current.mu.Unlock()
	s.removeSession(params.SessionID)
	return map[string]any{"rejected": true}, nil
}

func normalizeTimeout(minutes int) int {
	if minutes <= 0 || minutes > 60 {
		return 10
	}
	return minutes
}

func randomID(prefix string) string {
	return fmt.Sprintf("%s-%d", prefix, time.Now().UnixNano())
}
