package lan

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
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
	encryptor, err := NewChunkedEncryptor(wheSecret)
	if err != nil {
		t.Fatal(err)
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
