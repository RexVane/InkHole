package lan

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const transferArtifactMaxAge = 7 * 24 * time.Hour

type outgoingTransferRecord struct {
	TransferID string `json:"transfer_id"`
	UpdatedAt  int64  `json:"updated_at"`
}

var outgoingStateMu sync.Mutex

// NewTransferID returns an unpredictable WHPP transfer identifier. A new user
// send gets a new id, while retries reuse the id persisted in the outgoing
// state file so receiver receipts cannot turn a later send into a false success.
func NewTransferID() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

func transferIDForSend(cfg SenderConfig, target SendTarget, sourcePath, kind,
	name string, plainSize int64, digest string) (string, string, error) {
	if explicit := strings.ToLower(strings.TrimSpace(cfg.TransferID)); explicit != "" {
		if !ValidSHA256(explicit) {
			return "", "", errors.New("transfer id is invalid")
		}
		return explicit, "", nil
	}
	if strings.TrimSpace(cfg.OutgoingStatePath) == "" {
		id, err := NewTransferID()
		return id, "", err
	}

	absolute, err := filepath.Abs(sourcePath)
	if err != nil {
		return "", "", err
	}
	peerKey := strings.ToLower(strings.TrimSpace(target.InstanceID))
	if peerKey == "" {
		peerKey = net.JoinHostPort(target.Host, strconv.Itoa(target.Port))
	}
	keyRaw, err := json.Marshal([]any{
		absolute, kind, name, plainSize, digest, peerKey,
	})
	if err != nil {
		return "", "", err
	}
	keySum := sha256.Sum256(keyRaw)
	key := hex.EncodeToString(keySum[:])

	outgoingStateMu.Lock()
	defer outgoingStateMu.Unlock()
	state := loadOutgoingState(cfg.OutgoingStatePath)
	cutoff := time.Now().Add(-transferArtifactMaxAge).Unix()
	for existingKey, record := range state {
		if record.UpdatedAt < cutoff || !ValidSHA256(record.TransferID) {
			delete(state, existingKey)
		}
	}
	record := state[key]
	if !ValidSHA256(record.TransferID) {
		record.TransferID, err = NewTransferID()
		if err != nil {
			return "", "", err
		}
	}
	record.UpdatedAt = time.Now().Unix()
	state[key] = record
	if err := saveOutgoingState(cfg.OutgoingStatePath, state); err != nil {
		return "", "", err
	}
	return record.TransferID, key, nil
}

func completeOutgoingTransfer(path, key string) error {
	if strings.TrimSpace(path) == "" || key == "" {
		return nil
	}
	outgoingStateMu.Lock()
	defer outgoingStateMu.Unlock()
	state := loadOutgoingState(path)
	if _, exists := state[key]; !exists {
		return nil
	}
	delete(state, key)
	if len(state) == 0 {
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		return nil
	}
	return saveOutgoingState(path, state)
}

func loadOutgoingState(path string) map[string]outgoingTransferRecord {
	state := make(map[string]outgoingTransferRecord)
	raw, err := os.ReadFile(path)
	if err == nil {
		_ = json.Unmarshal(raw, &state)
	}
	return state
}

func saveOutgoingState(path string, state map[string]outgoingTransferRecord) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(state)
	if err != nil {
		return err
	}
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, raw, 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, path); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}
