package lan

import (
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	sendInstanceID = "11111111111111111111111111111111"
	recvInstanceID = "22222222222222222222222222222222"
)

type transferHarness struct {
	sender   *Identity
	receiver *Identity
	recv     *Receiver
	inbox    string
	addr     *net.TCPAddr
	received chan string
	listener net.Listener
}

// startTransferHarness runs a WHPP receiver behind the same magic dispatch
// production uses.
func startTransferHarness(t *testing.T, secret string) *transferHarness {
	t.Helper()
	senderIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	receiverIdentity, err := GenerateIdentity()
	if err != nil {
		t.Fatal(err)
	}
	inbox := t.TempDir()
	received := make(chan string, 4)
	receiver, err := NewReceiver(ReceiverConfig{
		InboxDir:   inbox,
		Secret:     secret,
		Identity:   receiverIdentity,
		InstanceID: recvInstanceID,
		OnReceived: func(path string) { received <- path },
	})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func(conn net.Conn) {
				head := make([]byte, 4)
				if _, err := io.ReadFull(conn, head); err != nil {
					_ = conn.Close()
					return
				}
				switch string(head) {
				case whppMagicStr:
					receiver.HandleWHPP(conn)
				case capMagic:
					_ = RespondProbe(conn, receiverIdentity, recvInstanceID,
						"收端", []string{CapReliable})
					_ = conn.Close()
				default:
					_ = conn.Close()
				}
			}(conn)
		}
	}()
	return &transferHarness{
		sender:   senderIdentity,
		receiver: receiverIdentity,
		recv:     receiver,
		inbox:    inbox,
		addr:     listener.Addr().(*net.TCPAddr),
		received: received,
		listener: listener,
	}
}

func (h *transferHarness) target() SendTarget {
	return SendTarget{
		Host:        "127.0.0.1",
		Port:        h.addr.Port,
		InstanceID:  recvInstanceID,
		Fingerprint: h.receiver.Fingerprint,
	}
}

func (h *transferHarness) senderConfig(secret string) SenderConfig {
	return SenderConfig{
		Secret:     secret,
		Identity:   h.sender,
		InstanceID: sendInstanceID,
	}
}

func writeTempFile(t *testing.T, size int) (string, []byte) {
	t.Helper()
	payload := make([]byte, size)
	if _, err := rand.Read(payload); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "样本文件 v1.bin")
	if err := os.WriteFile(path, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	return path, payload
}

func waitReceived(t *testing.T, h *transferHarness) string {
	t.Helper()
	select {
	case path := <-h.received:
		return path
	case <-time.After(10 * time.Second):
		t.Fatal("receiver callback never fired")
		return ""
	}
}

func assertDelivered(t *testing.T, h *transferHarness, payload []byte) string {
	t.Helper()
	path := waitReceived(t, h)
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatal("delivered payload mismatch")
	}
	leftovers, err := filepath.Glob(filepath.Join(h.inbox, ".inkhole-*"))
	if err != nil {
		t.Fatal(err)
	}
	for _, leftover := range leftovers {
		if !strings.HasSuffix(leftover, ".done.json") {
			t.Fatalf("checkpoint not cleaned up: %s", leftover)
		}
	}
	return path
}

func TestSendReceivePlaintext(t *testing.T) {
	h := startTransferHarness(t, "")
	path, payload := writeTempFile(t, 3_000_000)
	if err := SendFile(context.Background(), h.target(), path,
		h.senderConfig("")); err != nil {
		t.Fatalf("SendFile: %v", err)
	}
	delivered := assertDelivered(t, h, payload)
	if filepath.Base(delivered) != "样本文件 v1.bin" {
		t.Fatalf("delivered name = %s", filepath.Base(delivered))
	}
}

func TestSendReceiveEncrypted(t *testing.T) {
	h := startTransferHarness(t, "共享口令")
	path, payload := writeTempFile(t, 5_000_000) // crosses one chunk boundary
	if err := SendFile(context.Background(), h.target(), path,
		h.senderConfig("共享口令")); err != nil {
		t.Fatalf("SendFile: %v", err)
	}
	assertDelivered(t, h, payload)
}

func TestSendRejectedOnSecretMismatch(t *testing.T) {
	h := startTransferHarness(t, "") // receiver has no secret
	path, _ := writeTempFile(t, 1000)
	err := SendFile(context.Background(), h.target(), path,
		h.senderConfig("发送端口令"))
	if err == nil {
		t.Fatal("send succeeded despite receiver lacking a secret")
	}
}

func TestSendIdempotentResend(t *testing.T) {
	h := startTransferHarness(t, "")
	path, payload := writeTempFile(t, 100_000)
	if err := SendFile(context.Background(), h.target(), path,
		h.senderConfig("")); err != nil {
		t.Fatal(err)
	}
	assertDelivered(t, h, payload)
	// A full resend (lost-ACK replay) must succeed without a second copy.
	if err := SendFile(context.Background(), h.target(), path,
		h.senderConfig("")); err != nil {
		t.Fatalf("idempotent resend: %v", err)
	}
	entries, err := filepath.Glob(filepath.Join(h.inbox, "样本文件*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("resend duplicated the file: %v", entries)
	}
}

func TestResumeAfterInterruptedTransfer(t *testing.T) {
	h := startTransferHarness(t, "")
	path, payload := writeTempFile(t, 4_000_000)
	digest, err := SHA256File(path)
	if err != nil {
		t.Fatal(err)
	}
	filename := filepath.Base(path)
	header := &TransferHeader{
		Version:          ProtocolVersion,
		Filename:         filename,
		PlainSize:        int64(len(payload)),
		TransferID:       TransferID(KindFile, filename, int64(len(payload)), digest),
		SHA256:           digest,
		Kind:             KindFile,
		MtimeMS:          time.Now().UnixMilli(),
		WantACK:          true,
		SenderInstanceID: sendInstanceID,
		SenderPublicKey:  h.sender.PublicKey,
	}
	// Hand-drive half a transfer, then cut the connection.
	conn, err := net.Dial("tcp", h.addr.String())
	if err != nil {
		t.Fatal(err)
	}
	headerRaw, err := encodeHeaderJSON(header)
	if err != nil {
		t.Fatal(err)
	}
	frame := append([]byte(whppMagicStr),
		[]byte{0, 0, byte(len(headerRaw) >> 8), byte(len(headerRaw))}...)
	if _, err := conn.Write(append(frame, headerRaw...)); err != nil {
		t.Fatal(err)
	}
	marker, err := readExact(conn, 1)
	if err != nil || marker[0] != resume {
		t.Fatalf("no resume marker: %v %v", marker, err)
	}
	offsetRaw, err := readExact(conn, 8)
	if err != nil {
		t.Fatal(err)
	}
	_ = offsetRaw
	nonce, err := readExact(conn, nonceSize)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := readExact(conn, 32); err != nil { // receiver instance
		t.Fatal(err)
	}
	if _, err := readSizedField(conn); err != nil { // receiver pk
		t.Fatal(err)
	}
	if _, err := readSizedField(conn); err != nil { // receiver sig
		t.Fatal(err)
	}
	message, err := TransferMessage(nonce, header, 0)
	if err != nil {
		t.Fatal(err)
	}
	signature, err := h.sender.Sign(message)
	if err != nil {
		t.Fatal(err)
	}
	sigFrame := []byte{0, byte(len(signature))}
	if _, err := conn.Write(append(sigFrame, signature...)); err != nil {
		t.Fatal(err)
	}
	sizeFrame := make([]byte, 8)
	size := uint64(len(payload))
	for i := 0; i < 8; i++ {
		sizeFrame[7-i] = byte(size >> (8 * i))
	}
	if _, err := conn.Write(sizeFrame); err != nil {
		t.Fatal(err)
	}
	half := len(payload) / 2
	if _, err := conn.Write(payload[:half]); err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()

	// The .part checkpoint must survive with exactly the delivered bytes.
	deadline := time.Now().Add(5 * time.Second)
	partGlob := filepath.Join(h.inbox, ".inkhole-*.part")
	for {
		parts, _ := filepath.Glob(partGlob)
		if len(parts) == 1 {
			if info, err := os.Stat(parts[0]); err == nil &&
				info.Size() == int64(half) {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("checkpoint with half payload never appeared")
		}
		time.Sleep(50 * time.Millisecond)
	}

	// A normal SendFile now resumes from the checkpoint and completes.
	if err := SendFile(context.Background(), h.target(), path,
		h.senderConfig("")); err != nil {
		t.Fatalf("resume send: %v", err)
	}
	assertDelivered(t, h, payload)
}

func TestSendRefusesWrongReceiverIdentity(t *testing.T) {
	h := startTransferHarness(t, "")
	path, _ := writeTempFile(t, 1000)
	target := h.target()
	target.InstanceID = "33333333333333333333333333333333"
	err := SendFile(context.Background(), target, path, h.senderConfig(""))
	if err == nil || errors.Is(err, ErrReceiverRejected) {
		t.Fatalf("expected identity failure, got %v", err)
	}
}

func TestTransferIDMatchesPython(t *testing.T) {
	// python: p2p._transfer_id("file", "样本 <文件>.bin", 12345,
	//   "aa"*32) == pinned below
	got := TransferID("file", "样本 <文件>.bin", 12345, strings.Repeat("aa", 32))
	if got != pyTransferID {
		t.Fatalf("TransferID = %s, want %s", got, pyTransferID)
	}
}

// pinned by the Python reference — see test above.
const pyTransferID = "6540d9dc0350d1dcd2074e3f780934110829f4d6ea0c4f2538c38c581419e3cd"
