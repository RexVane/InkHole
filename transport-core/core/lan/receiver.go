package lan

import (
	"bufio"
	"crypto/hmac"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	recvIdleTimeout = 300 * time.Second
	diskMargin      = 256 * 1024 * 1024
	recvBuffer      = 1024 * 1024
)

// ReceiverConfig wires one WHPP receiver to its environment.
type ReceiverConfig struct {
	InboxDir   string
	Secret     string // end-to-end secret; "" rejects encrypted transfers
	Identity   *Identity
	InstanceID string
	OnProgress func(filename string, done, total int64)
	OnReceived func(path string)
	OnStatus   func(msg string)
}

// Receiver accepts WHPP v3 transactions. The first Go milestone covers
// kind=file (folders announce as unsupported, senders fall back to ZIP,
// exactly like talking to a pre-folder desktop build).
type Receiver struct {
	cfg  ReceiverConfig
	mu   sync.Mutex
	cond *sync.Cond
	busy map[string]bool
}

// Selecting a unique destination and publishing the verified checkpoint must
// be one process-wide operation. Multiple LAN sessions may share an inbox.
var receiveCommitMu sync.Mutex

func NewReceiver(cfg ReceiverConfig) (*Receiver, error) {
	if cfg.Identity == nil || !ValidInstanceID(strings.ToLower(cfg.InstanceID)) {
		return nil, errors.New("receiver identity and instance id are required")
	}
	if cfg.InboxDir == "" {
		return nil, errors.New("inbox directory is required")
	}
	cfg.InstanceID = strings.ToLower(cfg.InstanceID)
	if err := os.MkdirAll(cfg.InboxDir, 0o755); err != nil {
		return nil, fmt.Errorf("create inbox directory: %w", err)
	}
	cleanupTransferArtifacts(cfg.InboxDir, time.Now())
	receiver := &Receiver{cfg: cfg, busy: make(map[string]bool)}
	receiver.cond = sync.NewCond(&receiver.mu)
	return receiver, nil
}

func (r *Receiver) status(format string, args ...any) {
	if r.cfg.OnStatus != nil {
		r.cfg.OnStatus(fmt.Sprintf(format, args...))
	}
}

// SafeFilename mirrors p2p._safe_filename: strip directories from either
// separator convention, neutralize Windows-illegal characters, trim
// trailing dots/spaces, never return empty.
func SafeFilename(raw string) string {
	name := raw
	if idx := strings.LastIndexAny(name, "/\\"); idx >= 0 {
		name = name[idx+1:]
	}
	var builder strings.Builder
	for _, r := range name {
		if r < 0x20 || strings.ContainsRune(`<>:"|?*`, r) {
			builder.WriteRune('_')
		} else {
			builder.WriteRune(r)
		}
	}
	name = strings.TrimRight(builder.String(), ". ")
	if name == "" || name == "." || name == ".." {
		return "unknown"
	}
	return name
}

func uniquePath(root, filename string) string {
	base := filename
	ext := ""
	if dot := strings.LastIndexByte(filename, '.'); dot > 0 {
		base, ext = filename[:dot], filename[dot:]
	}
	candidate := filepath.Join(root, filename)
	for counter := 2; ; counter++ {
		if _, err := os.Lstat(candidate); errors.Is(err, os.ErrNotExist) {
			return candidate
		}
		candidate = filepath.Join(root, fmt.Sprintf("%s (%d)%s", base, counter, ext))
	}
}

func loadJSONFile(path string) map[string]any {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var decoded map[string]any
	if json.Unmarshal(raw, &decoded) != nil {
		return nil
	}
	return decoded
}

func writeJSONAtomic(path string, value map[string]any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func metadataMatches(saved map[string]any, expected map[string]any) bool {
	if len(saved) == 0 {
		return false
	}
	for key, want := range expected {
		got, ok := saved[key]
		if !ok {
			return false
		}
		switch wantValue := want.(type) {
		case string:
			text, ok := got.(string)
			if !ok || text != wantValue {
				return false
			}
		case int64:
			number, ok := got.(float64)
			if !ok || int64(number) != wantValue {
				return false
			}
		case int:
			number, ok := got.(float64)
			if !ok || int(number) != wantValue {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func readExact(conn net.Conn, size int) ([]byte, error) {
	buffer := make([]byte, size)
	if _, err := io.ReadFull(conn, buffer); err != nil {
		return nil, err
	}
	return buffer, nil
}

type recvError struct{ reason string }

func (e *recvError) Error() string { return e.reason }

func reject(format string, args ...any) error {
	return &recvError{reason: fmt.Sprintf(format, args...)}
}

// HandleWHPP serves one transaction on a connection whose "WHPP" magic has
// already been consumed by the connection dispatcher.
func (r *Receiver) HandleWHPP(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(recvIdleTimeout))
	header, err := r.readHeader(conn)
	if err != nil {
		if !errors.Is(err, io.EOF) {
			r.status("拒收：%s", err)
		}
		return
	}
	filename := SafeFilename(header.Filename)
	transferStarted := false
	ackSent := false
	ok := false
	err = r.receive(conn, header, filename, &transferStarted, &ackSent, &ok)
	if err != nil && !ackSent {
		var netErr net.Error
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) ||
			errors.As(err, &netErr) {
			r.status("接收中断，已保留续传进度：%s", filename)
		} else {
			r.status("接收失败: %s", err)
		}
		if transferStarted {
			_, _ = conn.Write([]byte{ackFail})
			drainAfterReject(conn)
		}
	}
}

// drainAfterReject keeps a rejected connection readable long enough for the
// sender to notice the ackFail byte. Closing immediately RSTs the socket
// while the sender is still writing body frames, which can destroy the
// queued ack and turn an explicit "wrong secret" refusal into a generic
// connection-reset that the sender then uselessly retries.
func drainAfterReject(conn net.Conn) {
	if tcp, ok := conn.(*net.TCPConn); ok {
		_ = tcp.CloseWrite()
	}
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buffer := make([]byte, 64*1024)
	var drained int64
	for drained < 8*1024*1024 {
		n, err := conn.Read(buffer)
		drained += int64(n)
		if err != nil {
			return
		}
	}
}

func (r *Receiver) readHeader(conn net.Conn) (*TransferHeader, error) {
	sizeRaw, err := readExact(conn, 4)
	if err != nil {
		return nil, io.EOF
	}
	size := binary.BigEndian.Uint32(sizeRaw)
	if size == 0 || size > maxHeaderSize {
		return nil, reject("消息头大小非法")
	}
	raw, err := readExact(conn, int(size))
	if err != nil {
		return nil, io.EOF
	}
	var header TransferHeader
	if err := json.Unmarshal(raw, &header); err != nil {
		return nil, reject("无效的消息头格式")
	}
	header.TransferID = strings.ToLower(header.TransferID)
	header.SHA256 = strings.ToLower(header.SHA256)
	header.SenderInstanceID = strings.ToLower(header.SenderInstanceID)
	return &header, nil
}

func (r *Receiver) receive(conn net.Conn, header *TransferHeader,
	filename string, transferStarted, ackSent, committed *bool) error {
	if header.Version != ProtocolVersion || !header.WantACK {
		return reject("拒收 %s：需要 WHPP v3", filename)
	}
	if header.Kind != KindFile && header.Kind != KindFolder {
		return reject("拒收 %s：不支持的传输类型", filename)
	}
	if header.Kind == KindFolder && header.PlainSize < 8 {
		return reject("拒收 %s：文件大小非法", filename)
	}
	if header.PlainSize < 0 || header.PlainSize > maxFileSize {
		return reject("拒收 %s：文件大小非法", filename)
	}
	if !ValidSHA256(header.TransferID) || !ValidSHA256(header.SHA256) {
		return reject("拒收 %s：传输标识或摘要非法", filename)
	}
	if !ValidInstanceID(header.SenderInstanceID) {
		return reject("拒收 %s：发送设备身份非法", filename)
	}
	senderFingerprint, err := PublicFingerprint(header.SenderPublicKey)
	if err != nil {
		return reject("拒收 %s：发送设备公钥非法", filename)
	}
	if header.MtimeMS < 0 {
		return reject("拒收 %s：修改时间非法", filename)
	}
	if header.Encrypted && header.EncMode != "chunked" {
		return reject("拒收 %s：加密格式不支持续传", filename)
	}
	if header.Encrypted && r.cfg.Secret == "" {
		return reject("拒收 %s：对方启用了加密，本机未设口令", filename)
	}
	root := r.cfg.InboxDir
	if err := os.MkdirAll(root, 0o755); err != nil {
		return reject("拒收 %s：无法使用收件箱目录 (%s)", filename, err)
	}

	if !r.claimCheckpoint(header.TransferID) {
		return reject("拒收 %s：等待前一次传输结束超时", filename)
	}
	defer r.releaseCheckpoint(header.TransferID)

	base := filepath.Join(root, ".inkhole-"+header.TransferID)
	partPath := base + ".part"
	metaPath := base + ".json"
	donePath := base + ".done.json"
	commitPath := base + ".commit.json"
	metadata := map[string]any{
		"version":            ProtocolVersion,
		"filename":           filename,
		"plain_size":         header.PlainSize,
		"sha256":             header.SHA256,
		"kind":               header.Kind,
		"mtime_ms":           header.MtimeMS,
		"sender_instance_id": header.SenderInstanceID,
		"sender_fingerprint": senderFingerprint,
	}

	// Lost-ACK idempotency: a receipt proves the payload was committed.
	if r.finishIdempotent(conn, header, metadata, donePath, commitPath,
		metaPath, partPath, transferStarted, ackSent) {
		*committed = true
		return nil
	}

	if existing := loadJSONFile(metaPath); !metadataMatches(existing, metadata) {
		for _, stale := range []string{partPath, metaPath} {
			_ = os.Remove(stale)
		}
		if err := writeJSONAtomic(metaPath, metadata); err != nil {
			return reject("拒收 %s：无法写入检查点 (%s)", filename, err)
		}
	}
	var offset int64
	if info, err := os.Stat(partPath); err == nil {
		offset = info.Size()
		if offset > header.PlainSize {
			_ = os.Remove(partPath)
			offset = 0
		}
	}

	*transferStarted = true
	r.progress(filename, offset, header.PlainSize)
	nonce, err := r.sendChallenge(conn, header, offset)
	if err != nil {
		return err
	}
	if err := r.verifySender(conn, header, offset, nonce); err != nil {
		return err
	}
	bodyRaw, err := readExact(conn, 8)
	if err != nil {
		return fmt.Errorf("续传数据长度缺失: %w", err)
	}
	bodySize := int64(binary.BigEndian.Uint64(bodyRaw))
	remaining := header.PlainSize - offset
	expectedWire := remaining
	if header.Encrypted && remaining > 0 {
		expectedWire = ChunkedWireSize(remaining)
	}
	if bodySize != expectedWire {
		return errors.New("续传数据长度不一致")
	}

	if remaining > 0 {
		if err := r.receiveBody(conn, header, filename, partPath,
			offset, remaining, bodySize); err != nil {
			return err
		}
	} else if _, err := os.Stat(partPath); errors.Is(err, os.ErrNotExist) {
		if err := os.WriteFile(partPath, nil, 0o600); err != nil {
			return err
		}
	}

	info, err := os.Stat(partPath)
	if err != nil || info.Size() != header.PlainSize {
		return errors.New("文件数据不完整")
	}
	actual, err := SHA256File(partPath)
	if err != nil {
		return err
	}
	if !hmac.Equal([]byte(actual), []byte(header.SHA256)) {
		_ = os.Remove(partPath)
		_ = os.Remove(metaPath)
		return errors.New("文件 SHA-256 校验失败，已丢弃检查点")
	}

	receipt := make(map[string]any, len(metadata)+2)
	for key, value := range metadata {
		receipt[key] = value
	}
	var destination string
	if header.Kind == KindFolder {
		staging, err := r.extractFolderCheckpoint(root, partPath, metaPath)
		if err != nil {
			return err
		}
		receiveCommitMu.Lock()
		destination = uniqueDirPath(root, filename)
		receipt["path"] = destination
		receipt["completed_at"] = time.Now().Unix()
		if err := writeJSONAtomic(commitPath, receipt); err != nil {
			receiveCommitMu.Unlock()
			_ = os.RemoveAll(staging)
			return err
		}
		if err := os.Rename(staging, destination); err != nil {
			receiveCommitMu.Unlock()
			_ = os.RemoveAll(staging)
			return err
		}
		applyMtime(destination, header.MtimeMS)
		receiveCommitMu.Unlock()
		_ = os.Remove(partPath)
	} else {
		receiveCommitMu.Lock()
		destination = uniquePath(root, filename)
		receipt["path"] = destination
		receipt["completed_at"] = time.Now().Unix()
		if err := writeJSONAtomic(commitPath, receipt); err != nil {
			receiveCommitMu.Unlock()
			return err
		}
		if err := os.Rename(partPath, destination); err != nil {
			receiveCommitMu.Unlock()
			return err
		}
		applyMtime(destination, header.MtimeMS)
		receiveCommitMu.Unlock()
	}
	if err := writeJSONAtomic(donePath, receipt); err != nil {
		return err
	}
	_ = os.Remove(metaPath)
	_ = os.Remove(commitPath)

	digest, _ := hex.DecodeString(header.SHA256)
	if _, err := conn.Write(append([]byte{ackOK}, digest...)); err != nil {
		return err
	}
	*ackSent = true
	*committed = true
	if r.cfg.OnReceived != nil {
		r.cfg.OnReceived(destination)
	}
	r.status("已接收并校验：%s", filepath.Base(destination))
	return nil
}

func cleanupTransferArtifacts(root string, now time.Time) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return
	}
	cutoff := now.Add(-transferArtifactMaxAge)
	groups := make(map[string][]string)
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, ".inkhole-") || name == ".inkhole-outgoing.json" {
			continue
		}
		path := filepath.Join(root, name)
		rest := strings.TrimPrefix(name, ".inkhole-")
		transferID := strings.SplitN(rest, ".", 2)[0]
		if ValidSHA256(transferID) {
			groups[transferID] = append(groups[transferID], path)
			continue
		}
		if info, statErr := entry.Info(); statErr == nil && info.ModTime().Before(cutoff) {
			if entry.IsDir() {
				_ = os.RemoveAll(path)
			} else {
				_ = os.Remove(path)
			}
		}
	}
	for _, paths := range groups {
		newest := time.Time{}
		for _, path := range paths {
			if info, statErr := os.Stat(path); statErr == nil && info.ModTime().After(newest) {
				newest = info.ModTime()
			}
		}
		if newest.IsZero() || !newest.Before(cutoff) {
			continue
		}
		for _, path := range paths {
			if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
				_ = os.RemoveAll(path)
			} else {
				_ = os.Remove(path)
			}
		}
	}
}

// finishIdempotent replays the committed outcome for retries of an already
// delivered transfer (receipt present, or a crash between rename and
// receipt that the commit journal can prove).
func (r *Receiver) finishIdempotent(conn net.Conn, header *TransferHeader,
	metadata map[string]any, donePath, commitPath, metaPath, partPath string,
	transferStarted, ackSent *bool) bool {
	var recovered string
	commit := loadJSONFile(commitPath)
	if commit != nil {
		recovered = r.validCommitDestination(commit, metadata)
		if recovered == "" {
			_ = os.Remove(commitPath)
		}
	}
	done := loadJSONFile(donePath)
	if !metadataMatches(done, metadata) && recovered == "" {
		return false
	}
	*transferStarted = true
	nonce, err := r.sendChallenge(conn, header, header.PlainSize)
	if err != nil {
		return true
	}
	if err := r.verifySender(conn, header, header.PlainSize, nonce); err != nil {
		return true
	}
	bodyRaw, err := readExact(conn, 8)
	if err != nil || binary.BigEndian.Uint64(bodyRaw) != 0 {
		return true
	}
	if recovered != "" {
		applyMtime(recovered, header.MtimeMS)
		_ = writeJSONAtomic(donePath, commit)
	}
	_ = os.Remove(metaPath)
	_ = os.Remove(commitPath)
	digest, _ := hex.DecodeString(header.SHA256)
	if _, err := conn.Write(append([]byte{ackOK}, digest...)); err != nil {
		return true
	}
	*ackSent = true
	if recovered != "" {
		if r.cfg.OnReceived != nil {
			r.cfg.OnReceived(recovered)
		}
		r.status("已恢复并校验：%s", filepath.Base(recovered))
	}
	return true
}

// validCommitDestination accepts a commit journal only when it matches this
// header and its destination actually holds the fully verified payload.
func (r *Receiver) validCommitDestination(commit map[string]any,
	metadata map[string]any) string {
	if !metadataMatches(commit, metadata) {
		return ""
	}
	path, _ := commit["path"].(string)
	if path == "" {
		return ""
	}
	rootAbs, err := filepath.Abs(r.cfg.InboxDir)
	if err != nil {
		return ""
	}
	pathAbs, err := filepath.Abs(path)
	if err != nil {
		return ""
	}
	if pathAbs != rootAbs && !strings.HasPrefix(pathAbs, rootAbs+string(os.PathSeparator)) {
		return ""
	}
	info, err := os.Stat(pathAbs)
	if err != nil {
		return ""
	}
	if kind, _ := metadata["kind"].(string); kind == KindFolder {
		if !info.IsDir() {
			return ""
		}
		return pathAbs
	}
	if info.IsDir() {
		return ""
	}
	size, _ := metadata["plain_size"].(int64)
	if info.Size() != size {
		return ""
	}
	digest, err := SHA256File(pathAbs)
	if err != nil || digest != metadata["sha256"] {
		return ""
	}
	return pathAbs
}

// extractFolderCheckpoint unpacks a fully verified WHF1 .part into a fresh
// staging directory; a malformed stream discards the checkpoint entirely.
func (r *Receiver) extractFolderCheckpoint(root, partPath,
	metaPath string) (string, error) {
	raw := make([]byte, 8)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	staging := filepath.Join(root,
		".inkhole-"+hex.EncodeToString(raw)+".folder.part")
	if err := os.Mkdir(staging, 0o755); err != nil {
		return "", err
	}
	file, err := os.Open(partPath)
	if err != nil {
		_ = os.RemoveAll(staging)
		return "", err
	}
	defer file.Close()
	if err := extractFolderStream(bufio.NewReaderSize(file, recvBuffer),
		staging); err != nil {
		_ = os.RemoveAll(staging)
		_ = os.Remove(partPath)
		_ = os.Remove(metaPath)
		return "", err
	}
	return staging, nil
}

func uniqueDirPath(root, name string) string {
	candidate := filepath.Join(root, name)
	for counter := 2; ; counter++ {
		if _, err := os.Lstat(candidate); errors.Is(err, os.ErrNotExist) {
			return candidate
		}
		candidate = filepath.Join(root, fmt.Sprintf("%s (%d)", name, counter))
	}
}

func (r *Receiver) claimCheckpoint(transferID string) bool {
	deadline := time.Now().Add(recvIdleTimeout)
	r.mu.Lock()
	defer r.mu.Unlock()
	for r.busy[transferID] {
		if time.Now().After(deadline) {
			return false
		}
		waitCond(r.cond, time.Second)
	}
	r.busy[transferID] = true
	return true
}

func waitCond(cond *sync.Cond, timeout time.Duration) {
	timer := time.AfterFunc(timeout, cond.Broadcast)
	defer timer.Stop()
	cond.Wait()
}

func (r *Receiver) releaseCheckpoint(transferID string) {
	r.mu.Lock()
	delete(r.busy, transferID)
	r.mu.Unlock()
	r.cond.Broadcast()
}

func (r *Receiver) progress(filename string, done, total int64) {
	if r.cfg.OnProgress != nil {
		r.cfg.OnProgress(filename, done, total)
	}
}

func (r *Receiver) sendChallenge(conn net.Conn, header *TransferHeader,
	offset int64) ([]byte, error) {
	nonce := make([]byte, nonceSize)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	message, err := ReceiverMessage(nonce, header, offset, r.cfg.InstanceID)
	if err != nil {
		return nil, err
	}
	signature, err := r.cfg.Identity.Sign(message)
	if err != nil {
		return nil, err
	}
	publicKey := []byte(r.cfg.Identity.PublicKey)
	signatureRaw := []byte(signature)
	if len(publicKey) > maxIdentityBytes || len(signatureRaw) > maxIdentityBytes {
		return nil, errors.New("接收设备身份字段过大")
	}
	frame := []byte{resume}
	frame = binary.BigEndian.AppendUint64(frame, uint64(offset))
	frame = append(frame, nonce...)
	frame = append(frame, r.cfg.InstanceID...)
	frame = binary.BigEndian.AppendUint16(frame, uint16(len(publicKey)))
	frame = append(frame, publicKey...)
	frame = binary.BigEndian.AppendUint16(frame, uint16(len(signatureRaw)))
	frame = append(frame, signatureRaw...)
	if _, err := conn.Write(frame); err != nil {
		return nil, err
	}
	return nonce, nil
}

func (r *Receiver) verifySender(conn net.Conn, header *TransferHeader,
	offset int64, nonce []byte) error {
	sizeRaw, err := readExact(conn, 2)
	if err != nil {
		return fmt.Errorf("发送设备签名缺失: %w", err)
	}
	size := binary.BigEndian.Uint16(sizeRaw)
	if size == 0 || size > 256 {
		return errors.New("发送设备签名大小非法")
	}
	signature, err := readExact(conn, int(size))
	if err != nil {
		return fmt.Errorf("发送设备签名缺失: %w", err)
	}
	message, err := TransferMessage(nonce, header, offset)
	if err != nil {
		return err
	}
	if !Verify(header.SenderPublicKey, message, string(signature)) {
		return errors.New("发送设备身份签名无效")
	}
	return nil
}

func (r *Receiver) receiveBody(conn net.Conn, header *TransferHeader,
	filename, partPath string, offset, remaining, bodySize int64) error {
	output, err := os.OpenFile(partPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer output.Close()
	var appended int64
	if header.Encrypted {
		headerRaw, err := readExact(conn, 32)
		if err != nil {
			return fmt.Errorf("加密流头不完整: %w", err)
		}
		decryptor, err := NewChunkedDecryptor(r.cfg.Secret, headerRaw)
		if err != nil {
			return err
		}
		consumed := int64(32)
		for consumed < bodySize {
			sizeRaw, err := readExact(conn, 4)
			if err != nil {
				return fmt.Errorf("加密数据不完整: %w", err)
			}
			frameSize := int64(binary.BigEndian.Uint32(sizeRaw))
			if frameSize < 16 || frameSize > ChunkSize+16 ||
				consumed+4+frameSize > bodySize {
				return errors.New("加密分块非法")
			}
			ciphertext, err := readExact(conn, int(frameSize))
			if err != nil {
				return fmt.Errorf("加密数据不完整: %w", err)
			}
			plain, ok := decryptor.DecryptChunk(ciphertext)
			if !ok {
				return errors.New("解密失败（两端口令不一致？）")
			}
			if appended+int64(len(plain)) > remaining {
				return errors.New("解密数据超过声明大小")
			}
			if _, err := output.Write(plain); err != nil {
				return err
			}
			appended += int64(len(plain))
			consumed += 4 + frameSize
			r.progress(filename, offset+appended, header.PlainSize)
		}
		if consumed != bodySize || appended != remaining {
			return errors.New("加密数据不完整")
		}
	} else {
		buffer := make([]byte, recvBuffer)
		for appended < remaining {
			chunk := buffer
			if left := remaining - appended; left < int64(len(chunk)) {
				chunk = chunk[:left]
			}
			read, err := conn.Read(chunk)
			if read > 0 {
				if _, werr := output.Write(chunk[:read]); werr != nil {
					return werr
				}
				appended += int64(read)
				r.progress(filename, offset+appended, header.PlainSize)
			}
			if err != nil {
				if appended == remaining {
					break
				}
				return fmt.Errorf("文件数据不完整: %w", err)
			}
		}
	}
	if err := output.Sync(); err != nil {
		return err
	}
	return nil
}

func applyMtime(path string, mtimeMS int64) {
	if mtimeMS <= 0 {
		return
	}
	stamp := time.UnixMilli(mtimeMS)
	_ = os.Chtimes(path, stamp, stamp)
}
