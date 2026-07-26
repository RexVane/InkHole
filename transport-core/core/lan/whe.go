package lan

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"io"
	"sync"

	"golang.org/x/crypto/hkdf"
	"golang.org/x/crypto/pbkdf2"
)

// WHE end-to-end encryption, byte-compatible with crypto.py and Crypto.kt.
//
// Whole-blob WHE1 (small files):
//
//	"WHE1" salt(16) nonce(12) AES-256-GCM(ciphertext|tag)
//
// Chunked WHE2/WHE3/WHE4 (large files):
//
//	magic salt(16) base_nonce(12), then frames of [len(u32 BE)] ciphertext.
//	Chunk i uses nonce = base[0:4] || BE64(BE64(base[4:12])+i) and AAD =
//	BE64(i), so tampering, reordering and cross-file splicing all fail.
//
// WHE1/WHE2 derive keys with 100k PBKDF2 iterations (legacy compat);
// WHE3 uses 600k per stream. WHE4 — negotiated via the "whe4" WHPC
// capability — runs 600k PBKDF2 once per secret against a fixed
// application salt (cached master key), then derives every stream key
// with HKDF-SHA256(master, salt=stream_salt, info): one ~0.5s derivation
// per secret instead of per transfer, with identical stream isolation.

const (
	// ChunkSize is the plaintext chunk for WHE2/WHE3/WHE4 streams.
	ChunkSize = 4 * 1024 * 1024
	// chunkOverhead is per-frame cost: 4-byte length + 16-byte GCM tag.
	chunkOverhead = 20

	legacyIterations = 100_000
	wheIterations    = 600_000

	wheMasterSalt  = "INKHOLE-WHE4-MASTER-V1"
	wheStreamInfo  = "INKHOLE-WHE4-STREAM-V1"
	wheMasterBytes = 32
)

var (
	wheMagic1 = []byte("WHE1")
	wheMagic2 = []byte("WHE2")
	wheMagic3 = []byte("WHE3")
	wheMagic4 = []byte("WHE4")
)

// masterCache holds one PBKDF2 result per secret so WHE4 pays the 600k
// iterations once per process instead of once per transfer. Its keys are
// process-keyed digests, so the cache does not retain plaintext secrets as
// map keys that are easy to expose in diagnostics or memory inspection.
var masterCache = struct {
	sync.Mutex
	keyOnce sync.Once
	key     [32]byte
	keyOK   bool
	masters map[[32]byte][wheMasterBytes]byte
}{masters: make(map[[32]byte][wheMasterBytes]byte)}

func masterCacheKey(secret string) ([32]byte, bool) {
	masterCache.keyOnce.Do(func() {
		_, err := rand.Read(masterCache.key[:])
		masterCache.keyOK = err == nil
	})
	if !masterCache.keyOK {
		return [32]byte{}, false
	}
	digest := hmac.New(sha256.New, masterCache.key[:])
	_, _ = digest.Write([]byte(secret))
	var key [32]byte
	copy(key[:], digest.Sum(nil))
	return key, true
}

func masterKey(secret string) []byte {
	cacheKey, cacheOK := masterCacheKey(secret)
	if cacheOK {
		masterCache.Lock()
		defer masterCache.Unlock()
		if key, ok := masterCache.masters[cacheKey]; ok {
			return key[:]
		}
	}
	derived := pbkdf2.Key([]byte(secret), []byte(wheMasterSalt),
		wheIterations, wheMasterBytes, sha256.New)
	var key [wheMasterBytes]byte
	copy(key[:], derived)
	if cacheOK {
		masterCache.masters[cacheKey] = key
	}
	return key[:]
}

func streamKeyWHE4(secret string, salt []byte) ([]byte, error) {
	reader := hkdf.New(sha256.New, masterKey(secret), salt, []byte(wheStreamInfo))
	key := make([]byte, 32)
	if _, err := io.ReadFull(reader, key); err != nil {
		return nil, err
	}
	return key, nil
}

func deriveKey(secret string, salt []byte, iterations int) []byte {
	return pbkdf2.Key([]byte(secret), salt, iterations, 32, sha256.New)
}

func gcmForKey(key []byte) (cipher.AEAD, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func newGCM(secret string, salt []byte, iterations int) (cipher.AEAD, error) {
	return gcmForKey(deriveKey(secret, salt, iterations))
}

// IsEncrypted reports whether blob starts with the whole-blob magic.
func IsEncrypted(blob []byte) bool {
	return len(blob) >= 4 && string(blob[:4]) == string(wheMagic1)
}

// EncryptWHE1 seals plain into the whole-blob legacy format (still what
// small in-memory payloads use on the wire today).
func EncryptWHE1(secret string, plain []byte) ([]byte, error) {
	salt := make([]byte, 16)
	nonce := make([]byte, 12)
	if _, err := rand.Read(salt); err != nil {
		return nil, err
	}
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	gcm, err := newGCM(secret, salt, legacyIterations)
	if err != nil {
		return nil, err
	}
	out := make([]byte, 0, 4+16+12+len(plain)+16)
	out = append(out, wheMagic1...)
	out = append(out, salt...)
	out = append(out, nonce...)
	return gcm.Seal(out, nonce, plain, nil), nil
}

// DecryptWHE1 opens a whole-blob payload; ok is false for wrong secrets,
// tampered data or foreign formats.
func DecryptWHE1(secret string, blob []byte) ([]byte, bool) {
	if !IsEncrypted(blob) || len(blob) < 4+16+12+16 {
		return nil, false
	}
	gcm, err := newGCM(secret, blob[4:20], legacyIterations)
	if err != nil {
		return nil, false
	}
	plain, err := gcm.Open(nil, blob[20:32], blob[32:], nil)
	if err != nil {
		return nil, false
	}
	return plain, true
}

// ChunkedWireSize returns the on-wire size of a chunked stream for a given
// plaintext size, so senders can announce it in the WHPP header.
func ChunkedWireSize(plainSize int64) int64 {
	chunks := (plainSize + ChunkSize - 1) / ChunkSize
	return 32 + plainSize + chunks*chunkOverhead
}

func chunkNonce(base []byte, idx uint64) []byte {
	counter := binary.BigEndian.Uint64(base[4:12]) + idx
	nonce := make([]byte, 12)
	copy(nonce, base[:4])
	binary.BigEndian.PutUint64(nonce[4:], counter)
	return nonce
}

func chunkAAD(idx uint64) []byte {
	aad := make([]byte, 8)
	binary.BigEndian.PutUint64(aad, idx)
	return aad
}

// ChunkedEncryptor produces a WHE3/WHE4 stream: emit Header() first, then
// one EncryptChunk frame per plaintext chunk (any size up to ChunkSize).
type ChunkedEncryptor struct {
	gcm    cipher.AEAD
	base   []byte
	header []byte
	idx    uint64
}

// NewChunkedEncryptor seals a chunked stream. useWHE4 selects the
// HKDF-per-stream format and must only be true when the receiver
// advertised the CapWHE4 capability; false emits WHE3, which every v3
// peer understands.
func NewChunkedEncryptor(secret string, useWHE4 bool) (*ChunkedEncryptor, error) {
	salt := make([]byte, 16)
	base := make([]byte, 12)
	if _, err := rand.Read(salt); err != nil {
		return nil, err
	}
	if _, err := rand.Read(base); err != nil {
		return nil, err
	}
	magic := wheMagic3
	var gcm cipher.AEAD
	var err error
	if useWHE4 {
		magic = wheMagic4
		key, keyErr := streamKeyWHE4(secret, salt)
		if keyErr != nil {
			return nil, keyErr
		}
		gcm, err = gcmForKey(key)
	} else {
		gcm, err = newGCM(secret, salt, wheIterations)
	}
	if err != nil {
		return nil, err
	}
	header := make([]byte, 0, 32)
	header = append(header, magic...)
	header = append(header, salt...)
	header = append(header, base...)
	return &ChunkedEncryptor{gcm: gcm, base: base, header: header}, nil
}

// Header is the 32-byte stream header.
func (e *ChunkedEncryptor) Header() []byte { return e.header }

// EncryptChunk seals one plaintext chunk into a [len | ciphertext] frame.
func (e *ChunkedEncryptor) EncryptChunk(plain []byte) []byte {
	ciphertext := e.gcm.Seal(nil, chunkNonce(e.base, e.idx), plain, chunkAAD(e.idx))
	e.idx++
	frame := make([]byte, 0, 4+len(ciphertext))
	frame = binary.BigEndian.AppendUint32(frame, uint32(len(ciphertext)))
	return append(frame, ciphertext...)
}

// ChunkedDecryptor opens WHE2/WHE3/WHE4 streams in order.
type ChunkedDecryptor struct {
	gcm  cipher.AEAD
	base []byte
	idx  uint64
}

// NewChunkedDecryptor consumes the 32-byte stream header.
func NewChunkedDecryptor(secret string, header []byte) (*ChunkedDecryptor, error) {
	if len(header) != 32 {
		return nil, errors.New("bad chunked encryption stream header")
	}
	salt := header[4:20]
	var gcm cipher.AEAD
	var err error
	switch string(header[:4]) {
	case string(wheMagic4):
		key, keyErr := streamKeyWHE4(secret, salt)
		if keyErr != nil {
			return nil, keyErr
		}
		gcm, err = gcmForKey(key)
	case string(wheMagic3):
		gcm, err = newGCM(secret, salt, wheIterations)
	case string(wheMagic2):
		gcm, err = newGCM(secret, salt, legacyIterations)
	default:
		return nil, errors.New("bad chunked encryption stream header")
	}
	if err != nil {
		return nil, err
	}
	base := append([]byte(nil), header[20:32]...)
	return &ChunkedDecryptor{gcm: gcm, base: base}, nil
}

// DecryptChunk opens the next ciphertext frame body (without the 4-byte
// length prefix); ok is false on wrong secret, tamper or reorder.
func (d *ChunkedDecryptor) DecryptChunk(ciphertext []byte) ([]byte, bool) {
	plain, err := d.gcm.Open(nil, chunkNonce(d.base, d.idx), ciphertext, chunkAAD(d.idx))
	if err != nil {
		return nil, false
	}
	d.idx++
	return plain, true
}
