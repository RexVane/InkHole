package lan

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"testing"
)

// Ciphertext vectors produced by the Python reference (crypto.py) with
// secret "测试口令-topsecret".
const (
	wheSecret   = "测试口令-topsecret"
	pyWHE1Hex   = "5748453143fbd509f1c6b8e07e1ff5f5594a6452493e4e001d8474de4c71b3cc61ca1bda62dc4c356052e6efc54febcea793aa4be3841646f15854df8df10ded8620"
	pyWHE1Plain = "hello inkhole WHE1"
	pyWHE3Hex   = "57484533c955b4167bcc02928351079a3e1020c2f03768b18da87797a30a551e00000020b702c753bc7ff3b476abb1316e7226c7b3f5ab6ebd65facafccb3dae177e8cb50000001a842cb835f4a6bd65106574b6d92ce9c75563d7aaefa70b23e260"
	pyWHE3Plain = "0123456789abcdefGHIJKLMNOP"
)

func TestDecryptPythonWHE1(t *testing.T) {
	blob, err := hex.DecodeString(pyWHE1Hex)
	if err != nil {
		t.Fatal(err)
	}
	if !IsEncrypted(blob) {
		t.Fatal("IsEncrypted = false for WHE1 blob")
	}
	plain, ok := DecryptWHE1(wheSecret, blob)
	if !ok || string(plain) != pyWHE1Plain {
		t.Fatalf("DecryptWHE1 = %q, %v", plain, ok)
	}
	if _, ok := DecryptWHE1("wrong", blob); ok {
		t.Fatal("wrong secret accepted")
	}
	tampered := append([]byte(nil), blob...)
	tampered[len(tampered)-1] ^= 1
	if _, ok := DecryptWHE1(wheSecret, tampered); ok {
		t.Fatal("tampered blob accepted")
	}
}

func TestWHE1RoundTrip(t *testing.T) {
	blob, err := EncryptWHE1(wheSecret, []byte(pyWHE1Plain))
	if err != nil {
		t.Fatal(err)
	}
	plain, ok := DecryptWHE1(wheSecret, blob)
	if !ok || string(plain) != pyWHE1Plain {
		t.Fatalf("round trip = %q, %v", plain, ok)
	}
}

// consumeChunkedStream splits a chunked stream into header + frame bodies.
func consumeChunkedStream(t *testing.T, stream []byte) ([]byte, [][]byte) {
	t.Helper()
	if len(stream) < 32 {
		t.Fatal("stream too short")
	}
	header, rest := stream[:32], stream[32:]
	var frames [][]byte
	for len(rest) > 0 {
		if len(rest) < 4 {
			t.Fatal("truncated frame length")
		}
		size := binary.BigEndian.Uint32(rest[:4])
		rest = rest[4:]
		if uint32(len(rest)) < size {
			t.Fatal("truncated frame body")
		}
		frames = append(frames, rest[:size])
		rest = rest[size:]
	}
	return header, frames
}

func TestDecryptPythonWHE3Stream(t *testing.T) {
	stream, err := hex.DecodeString(pyWHE3Hex)
	if err != nil {
		t.Fatal(err)
	}
	header, frames := consumeChunkedStream(t, stream)
	decryptor, err := NewChunkedDecryptor(wheSecret, header)
	if err != nil {
		t.Fatal(err)
	}
	var plain bytes.Buffer
	for _, frame := range frames {
		chunk, ok := decryptor.DecryptChunk(frame)
		if !ok {
			t.Fatal("chunk failed to decrypt")
		}
		plain.Write(chunk)
	}
	if plain.String() != pyWHE3Plain {
		t.Fatalf("plaintext = %q, want %q", plain.String(), pyWHE3Plain)
	}
	// Reordered frames must fail (chunk index is bound into nonce + AAD).
	reordered, err := NewChunkedDecryptor(wheSecret, header)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := reordered.DecryptChunk(frames[1]); ok {
		t.Fatal("out-of-order chunk accepted")
	}
}

func TestChunkedRoundTripAndWireSize(t *testing.T) {
	encryptor, err := NewChunkedEncryptor(wheSecret, false)
	if err != nil {
		t.Fatal(err)
	}
	if got := string(encryptor.Header()[:4]); got != "WHE3" {
		t.Fatalf("fallback stream magic = %q, want WHE3", got)
	}
	chunks := [][]byte{
		bytes.Repeat([]byte("A"), 1000),
		bytes.Repeat([]byte("B"), 17),
		[]byte("tail"),
	}
	stream := append([]byte(nil), encryptor.Header()...)
	for _, chunk := range chunks {
		stream = append(stream, encryptor.EncryptChunk(chunk)...)
	}
	header, frames := consumeChunkedStream(t, stream)
	decryptor, err := NewChunkedDecryptor(wheSecret, header)
	if err != nil {
		t.Fatal(err)
	}
	var plain bytes.Buffer
	for _, frame := range frames {
		chunk, ok := decryptor.DecryptChunk(frame)
		if !ok {
			t.Fatal("round-trip chunk failed")
		}
		plain.Write(chunk)
	}
	want := bytes.Join(chunks, nil)
	if !bytes.Equal(plain.Bytes(), want) {
		t.Fatal("round-trip plaintext mismatch")
	}
	if _, ok := decryptor.DecryptChunk(frames[0]); ok {
		t.Fatal("replayed chunk accepted")
	}

	// Wire-size math must match crypto.chunked_wire_size for full chunks.
	plainSize := int64(2*ChunkSize + 5)
	want3 := int64(32) + plainSize + 3*chunkOverhead
	if got := ChunkedWireSize(plainSize); got != want3 {
		t.Fatalf("ChunkedWireSize(%d) = %d, want %d", plainSize, got, want3)
	}
	if got := ChunkedWireSize(0); got != 32 {
		t.Fatalf("ChunkedWireSize(0) = %d, want 32", got)
	}
}

func TestWHE4RoundTripRejectsWrongSecretAndTamper(t *testing.T) {
	encryptor, err := NewChunkedEncryptor(wheSecret, true)
	if err != nil {
		t.Fatal(err)
	}
	header := append([]byte(nil), encryptor.Header()...)
	if got := string(header[:4]); got != "WHE4" {
		t.Fatalf("negotiated stream magic = %q, want WHE4", got)
	}
	frame := encryptor.EncryptChunk([]byte("WHE4 authenticated payload"))
	ciphertext := frame[4:]

	decryptor, err := NewChunkedDecryptor(wheSecret, header)
	if err != nil {
		t.Fatal(err)
	}
	plain, ok := decryptor.DecryptChunk(ciphertext)
	if !ok || string(plain) != "WHE4 authenticated payload" {
		t.Fatalf("WHE4 round trip = %q, %v", plain, ok)
	}

	wrong, err := NewChunkedDecryptor("wrong secret", header)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := wrong.DecryptChunk(ciphertext); ok {
		t.Fatal("WHE4 accepted the wrong secret")
	}

	tampered := append([]byte(nil), ciphertext...)
	tampered[len(tampered)-1] ^= 1
	tamperCheck, err := NewChunkedDecryptor(wheSecret, header)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := tamperCheck.DecryptChunk(tampered); ok {
		t.Fatal("WHE4 accepted tampered ciphertext")
	}
}

func TestSupportsWHE4RequiresExactCapability(t *testing.T) {
	if SupportsWHE4(nil) || SupportsWHE4([]string{"WHE4", "whe4-preview"}) {
		t.Fatal("WHE4 enabled without the exact negotiated capability")
	}
	if !SupportsWHE4([]string{CapReliable, CapWHE4}) {
		t.Fatal("WHE4 capability was not recognized")
	}
}

// TestWHE4RoundTrip covers the negotiated HKDF-per-stream format and its
// coexistence with WHE3 on the same secret.
func TestWHE4MasterCacheBoundedAndEvictionSafe(t *testing.T) {
	// Overflow the per-secret cache: the map must stay bounded, and an
	// evicted secret must still decrypt (re-derivation, not data loss).
	first := "边界口令-0"
	encryptor, err := NewChunkedEncryptor(first, true)
	if err != nil {
		t.Fatal(err)
	}
	frame := encryptor.EncryptChunk([]byte("payload"))
	for index := 1; index < wheMasterCacheMax+2; index++ {
		if _, err := NewChunkedEncryptor(fmt.Sprintf("边界口令-%d", index), true); err != nil {
			t.Fatal(err)
		}
	}
	masterCache.Lock()
	size := len(masterCache.masters)
	masterCache.Unlock()
	if size > wheMasterCacheMax {
		t.Fatalf("master cache grew to %d entries (max %d)", size, wheMasterCacheMax)
	}
	decryptor, err := NewChunkedDecryptor(first, encryptor.Header())
	if err != nil {
		t.Fatal(err)
	}
	if plain, ok := decryptor.DecryptChunk(frame[4:]); !ok || string(plain) != "payload" {
		t.Fatal("evicted secret no longer decrypts")
	}
}

func TestWHE4RoundTrip(t *testing.T) {
	encryptor, err := NewChunkedEncryptor(wheSecret, true)
	if err != nil {
		t.Fatal(err)
	}
	if string(encryptor.Header()[:4]) != "WHE4" {
		t.Fatalf("expected WHE4 magic, got %q", encryptor.Header()[:4])
	}
	payload := bytes.Repeat([]byte("whe4-负载"), 300)
	stream := append([]byte(nil), encryptor.Header()...)
	stream = append(stream, encryptor.EncryptChunk(payload)...)
	header, frames := consumeChunkedStream(t, stream)
	decryptor, err := NewChunkedDecryptor(wheSecret, header)
	if err != nil {
		t.Fatal(err)
	}
	plain, ok := decryptor.DecryptChunk(frames[0])
	if !ok || !bytes.Equal(plain, payload) {
		t.Fatal("WHE4 round-trip failed")
	}
	if _, ok := decryptor.DecryptChunk(frames[0]); ok {
		t.Fatal("WHE4 replayed chunk accepted")
	}
	// Wrong secret must fail cleanly.
	bad, err := NewChunkedDecryptor("wrong-secret", header)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := bad.DecryptChunk(frames[0]); ok {
		t.Fatal("WHE4 accepted a wrong secret")
	}
}

// TestWHE4KnownAnswer pins the cross-stack vector; crypto.py and Crypto.kt
// assert the identical bytes so the three stacks cannot drift.
func TestWHE4KnownAnswer(t *testing.T) {
	const streamHex = "57484534303132333435363738396162636465664b41546e6f6e63652f313242000000359ffa94d1a917a59c125e3cb007bbc7c4fea5ec27c482e87d9417ef98f5363211904eea1ba1f6147c5daf8a44400d341e6e7eec3e24"
	stream, err := hex.DecodeString(streamHex)
	if err != nil {
		t.Fatal(err)
	}
	header, frames := consumeChunkedStream(t, stream)
	decryptor, err := NewChunkedDecryptor("kat-秘密-2026", header)
	if err != nil {
		t.Fatal(err)
	}
	plain, ok := decryptor.DecryptChunk(frames[0])
	if !ok {
		t.Fatal("known-answer stream failed to decrypt")
	}
	if string(plain) != "墨洞 WHE4 known-answer test payload" {
		t.Fatalf("known-answer plaintext mismatch: %q", plain)
	}
}
