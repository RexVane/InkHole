package lan

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
)

// WHPP v3 — the resumable transfer protocol, byte-compatible with p2p.py
// and WHPP.kt. One TCP connection carries one transaction:
//
//	sender:   "WHPP" len(u32) header-JSON
//	receiver: RESUME(0x02) offset(u64) nonce(32) receiver_instance(32 ascii)
//	          pk_len(u16) pk  sig_len(u16) sig      — or ACK_FAIL(0x00)
//	sender:   sig_len(u16) sender_sig
//	sender:   body_size(u64) body
//	          body = plaintext, or WHE3 stream (32B header + frames) when
//	          header.encrypted; body_size covers exactly the remainder
//	          from the negotiated offset
//	receiver: ACK_OK(0x01) sha256(32 raw)          — or ACK_FAIL(0x00)
//
// Signatures: receiver signs ReceiverMessage(nonce, header, offset, its id);
// sender signs TransferMessage(nonce, header, offset). Nonce is chosen by
// the receiver, so a replayed sender signature never fits a new exchange.
//
// Resume state on the receiving side lives next to the inbox:
//
//	.inkhole-<transfer_id>.part         partial payload (append-only)
//	.inkhole-<transfer_id>.json         header metadata for mismatch checks
//	.inkhole-<transfer_id>.commit.json  written just before the atomic rename
//	.inkhole-<transfer_id>.done.json    receipt after a committed transfer

const (
	ProtocolVersion = 3

	whppMagicStr = "WHPP"
	KindFile     = "file"
	KindFolder   = "folder-v1"

	CapReliable = "reliable-v3"
	CapFolder   = KindFolder
	// CapWHE4 advertises the HKDF-per-stream encryption format. Senders
	// use WHE4 only for receivers listing it; everyone else gets WHE3.
	CapWHE4 = "whe4"

	maxHeaderSize    = 64 * 1024
	maxFileSize      = int64(1) << 40
	maxIdentityBytes = 512

	ackFail = 0x00
	ackOK   = 0x01
	resume  = 0x02
)

// TransferHeader is the WHPP v3 header JSON.
type TransferHeader struct {
	Version          int    `json:"version"`
	Filename         string `json:"filename"`
	PlainSize        int64  `json:"plain_size"`
	TransferID       string `json:"transfer_id"`
	SHA256           string `json:"sha256"`
	Kind             string `json:"kind"`
	MtimeMS          int64  `json:"mtime_ms"`
	Encrypted        bool   `json:"encrypted"`
	EncMode          string `json:"enc_mode,omitempty"`
	WantACK          bool   `json:"want_ack"`
	SenderInstanceID string `json:"sender_instance_id"`
	SenderPublicKey  string `json:"sender_public_key"`
}

// TransferID returns the legacy deterministic id used by early WHPP v3
// senders. New user sends use NewTransferID so repeating the same content is a
// distinct transaction; this helper remains for compatibility vectors and
// callers that need to identify an existing checkpoint.
//
// It mirrors p2p._transfer_id: sha256 of the canonical JSON array
// ["WHPP3", kind, name, plain_size, digest] encoded the way Python
// json.dumps(ensure_ascii=False, separators=(",", ":")) does — raw UTF-8,
// escaping only quotes, backslashes and control characters.
func TransferID(kind, name string, plainSize int64, digest string) string {
	buffer := []byte(`["WHPP3",`)
	buffer = appendJSONString(buffer, kind)
	buffer = append(buffer, ',')
	buffer = appendJSONString(buffer, name)
	buffer = append(buffer, ',')
	buffer = appendInt(buffer, plainSize)
	buffer = append(buffer, ',')
	buffer = appendJSONString(buffer, digest)
	buffer = append(buffer, ']')
	sum := sha256.Sum256(buffer)
	return hex.EncodeToString(sum[:])
}

// appendJSONString encodes a string the way Python json.dumps(ensure_ascii=
// False) does: raw UTF-8, escaping only ", \ and control chars.
func appendJSONString(buffer []byte, value string) []byte {
	buffer = append(buffer, '"')
	for _, r := range value {
		switch r {
		case '"':
			buffer = append(buffer, '\\', '"')
		case '\\':
			buffer = append(buffer, '\\', '\\')
		case '\n':
			buffer = append(buffer, '\\', 'n')
		case '\r':
			buffer = append(buffer, '\\', 'r')
		case '\t':
			buffer = append(buffer, '\\', 't')
		default:
			if r < 0x20 {
				buffer = append(buffer, fmt.Sprintf("\\u%04x", r)...)
			} else {
				buffer = append(buffer, string(r)...)
			}
		}
	}
	return append(buffer, '"')
}

func appendInt(buffer []byte, value int64) []byte {
	return fmt.Appendf(buffer, "%d", value)
}

// SupportsWHE4 reports whether a verified peer advertised the WHE4
// encryption capability.
func SupportsWHE4(capabilities []string) bool {
	for _, capability := range capabilities {
		if capability == CapWHE4 {
			return true
		}
	}
	return false
}

// ValidSHA256 mirrors p2p._valid_sha256.
func ValidSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, r := range value {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

// transferFields encodes the header fields shared by both signature
// messages (everything after domain+nonce in transfer_message).
func transferFields(header *TransferHeader, offset int64) ([]byte, error) {
	kind := []byte(header.Kind)
	filename := []byte(header.Filename)
	if len(kind) > 0xFFFF {
		return nil, errors.New("identity field is too long")
	}
	encrypted := byte(0)
	if header.Encrypted {
		encrypted = 1
	}
	var out []byte
	out = append(out, toLowerASCII(header.SenderInstanceID)...)
	out = append(out, toLowerASCII(header.TransferID)...)
	out = append(out, toLowerASCII(header.SHA256)...)
	out = binary.BigEndian.AppendUint16(out, uint16(len(kind)))
	out = append(out, kind...)
	out = binary.BigEndian.AppendUint32(out, uint32(len(filename)))
	out = append(out, filename...)
	out = binary.BigEndian.AppendUint64(out, uint64(header.PlainSize))
	out = binary.BigEndian.AppendUint64(out, uint64(header.MtimeMS))
	out = append(out, encrypted)
	out = binary.BigEndian.AppendUint64(out, uint64(offset))
	return out, nil
}

// TransferMessage mirrors device_identity.transfer_message.
func TransferMessage(nonce []byte, header *TransferHeader, offset int64) ([]byte, error) {
	if len(nonce) != nonceSize {
		return nil, errors.New("transfer nonce must be 32 bytes")
	}
	fields, err := transferFields(header, offset)
	if err != nil {
		return nil, err
	}
	out := make([]byte, 0, len(transferDomain)+len(nonce)+len(fields))
	out = append(out, transferDomain...)
	out = append(out, nonce...)
	return append(out, fields...), nil
}

// ReceiverMessage mirrors device_identity.receiver_message.
func ReceiverMessage(nonce []byte, header *TransferHeader, offset int64,
	receiverInstanceID string) ([]byte, error) {
	if len(nonce) != nonceSize {
		return nil, errors.New("receiver nonce must be 32 bytes")
	}
	receiver := toLowerASCII(receiverInstanceID)
	if len(receiver) != 32 {
		return nil, errors.New("receiver instance id must be 32 bytes")
	}
	fields, err := transferFields(header, offset)
	if err != nil {
		return nil, err
	}
	out := make([]byte, 0, len(receiverDomain)+len(nonce)+32+len(fields))
	out = append(out, receiverDomain...)
	out = append(out, nonce...)
	out = append(out, receiver...)
	return append(out, fields...), nil
}

func toLowerASCII(value string) []byte {
	out := []byte(value)
	for i, b := range out {
		if b >= 'A' && b <= 'Z' {
			out[i] = b + ('a' - 'A')
		}
	}
	return out
}

// SHA256File hashes a file start to end.
func SHA256File(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}
