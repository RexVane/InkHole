package core

import (
	"encoding/json"
	"errors"
	"fmt"
)

const ProtocolVersion = 1

type Request struct {
	ID     string          `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params,omitempty"`
}

type Response struct {
	ID     string `json:"id"`
	OK     bool   `json:"ok"`
	Result any    `json:"result,omitempty"`
	Error  string `json:"error,omitempty"`
}

type Event struct {
	Event string `json:"event"`
	Data  any    `json:"data,omitempty"`
}

type StartParams struct {
	LocalTarget string `json:"local_target"`
	LocalToken  string `json:"local_token"`
	DeviceName  string `json:"device_name"`
	InstanceID  string `json:"instance_id"`
}

func decodeParams(raw json.RawMessage, dst any) error {
	if len(raw) == 0 {
		return errors.New("missing params")
	}
	if err := json.Unmarshal(raw, dst); err != nil {
		return fmt.Errorf("invalid params: %w", err)
	}
	return nil
}

func encodeResponse(resp Response) string {
	b, err := json.Marshal(resp)
	if err != nil {
		fallback, _ := json.Marshal(Response{ID: resp.ID, OK: false, Error: err.Error()})
		return string(fallback)
	}
	return string(b)
}

func encodeEvent(event Event) string {
	b, _ := json.Marshal(event)
	return string(b)
}

// EncodeEvent is shared by the sidecar and gomobile adapters.
func EncodeEvent(event Event) string { return encodeEvent(event) }
