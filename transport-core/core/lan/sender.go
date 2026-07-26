package lan

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	sendIOTimeout   = 60 * time.Second
	transferRetries = 3
	connectTimeout  = 10 * time.Second
)

// ErrReceiverRejected marks a definitive refusal from the receiving side
// (bad secret, disk full, validation failure). Retrying cannot help.
var ErrReceiverRejected = errors.New("receiver rejected the transfer")

// SendTarget identifies one verified peer endpoint.
type SendTarget struct {
	Host          string
	Hosts         []string
	Port          int
	InstanceID    string // pins the receiver identity when non-empty
	Fingerprint   string // pins the receiver key when non-empty
	EndpointToken string // authenticates loopback SSH/wormhole bridge endpoints
	// UseWHE4 records that this receiver advertised CapWHE4; encrypted
	// sends then use the HKDF-per-stream format instead of WHE3.
	UseWHE4 bool
}

// SenderConfig wires one send operation to its environment.
type SenderConfig struct {
	Secret     string // non-empty enables end-to-end encryption
	Identity   *Identity
	InstanceID string
	OnProgress func(filename string, done, total int64)
	OnStatus   func(msg string)
}

// SendFile transfers one file over WHPP v3 with resume across retries.
func SendFile(ctx context.Context, target SendTarget, path string,
	cfg SenderConfig) error {
	if cfg.Identity == nil || !ValidInstanceID(strings.ToLower(cfg.InstanceID)) {
		return errors.New("sender identity and instance id are required")
	}
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	if info.IsDir() {
		return errors.New("directories are not supported yet; send a file")
	}
	if info.Size() > maxFileSize {
		return errors.New("file exceeds the 1TB protocol limit")
	}
	digest, err := SHA256File(path)
	if err != nil {
		return err
	}
	filename := filepath.Base(path)
	header := &TransferHeader{
		Version:          ProtocolVersion,
		Filename:         filename,
		PlainSize:        info.Size(),
		TransferID:       TransferID(KindFile, filename, info.Size(), digest),
		SHA256:           digest,
		Kind:             KindFile,
		MtimeMS:          info.ModTime().UnixMilli(),
		Encrypted:        cfg.Secret != "",
		WantACK:          true,
		SenderInstanceID: strings.ToLower(cfg.InstanceID),
		SenderPublicKey:  cfg.Identity.PublicKey,
	}
	if header.Encrypted {
		header.EncMode = "chunked"
	}
	return sendWithRetries(ctx, target, fileSource(path), header, cfg)
}

// SendFolder streams a directory as one WHF1 payload over WHPP v3.
func SendFolder(ctx context.Context, target SendTarget, path string,
	cfg SenderConfig) error {
	if cfg.Identity == nil || !ValidInstanceID(strings.ToLower(cfg.InstanceID)) {
		return errors.New("sender identity and instance id are required")
	}
	manifest, err := ScanFolder(path)
	if err != nil {
		return err
	}
	hasher := sha256.New()
	reader := NewFolderPayloadReader(manifest)
	if _, err := io.Copy(hasher, reader); err != nil {
		_ = reader.Close()
		return err
	}
	_ = reader.Close()
	digest := hex.EncodeToString(hasher.Sum(nil))
	name := filepath.Base(manifest.Root)
	if name == "" || name == "." || name == string(os.PathSeparator) {
		name = "folder"
	}
	header := &TransferHeader{
		Version:          ProtocolVersion,
		Filename:         name,
		PlainSize:        manifest.PlainSize,
		TransferID:       TransferID(KindFolder, name, manifest.PlainSize, digest),
		SHA256:           digest,
		Kind:             KindFolder,
		MtimeMS:          manifest.RootMtimeMS,
		Encrypted:        cfg.Secret != "",
		WantACK:          true,
		SenderInstanceID: strings.ToLower(cfg.InstanceID),
		SenderPublicKey:  cfg.Identity.PublicKey,
	}
	if header.Encrypted {
		header.EncMode = "chunked"
	}
	return sendWithRetries(ctx, target, folderSource(manifest), header, cfg)
}

type sourceFactory func(offset int64) (io.ReadCloser, error)

func fileSource(path string) sourceFactory {
	return func(offset int64) (io.ReadCloser, error) {
		file, err := os.Open(path)
		if err != nil {
			return nil, err
		}
		if _, err := file.Seek(offset, io.SeekStart); err != nil {
			_ = file.Close()
			return nil, err
		}
		return file, nil
	}
}

// folderSource regenerates the WHF1 stream and discards up to the resume
// offset; the manifest pins sizes so a mid-scan mutation fails loudly.
func folderSource(manifest *FolderManifest) sourceFactory {
	return func(offset int64) (io.ReadCloser, error) {
		reader := NewFolderPayloadReader(manifest)
		if offset > 0 {
			if _, err := io.CopyN(io.Discard, reader, offset); err != nil {
				_ = reader.Close()
				return nil, err
			}
		}
		return reader, nil
	}
}

func sendWithRetries(ctx context.Context, target SendTarget,
	source sourceFactory, header *TransferHeader, cfg SenderConfig) error {
	var lastErr error
	for attempt := 0; attempt < transferRetries; attempt++ {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		err := sendOnce(ctx, target, source, header, cfg)
		if err == nil {
			return nil
		}
		if errors.Is(err, ErrReceiverRejected) || ctx.Err() != nil {
			return err
		}
		lastErr = err
		if attempt+1 < transferRetries {
			if cfg.OnStatus != nil {
				cfg.OnStatus(fmt.Sprintf("连接中断，正在恢复传输（%d/%d）",
					attempt+2, transferRetries))
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt+1) * 500 * time.Millisecond):
			}
		}
	}
	return lastErr
}

func dialTarget(ctx context.Context, target SendTarget) (net.Conn, error) {
	hosts := dedupeStrings(append([]string{target.Host}, target.Hosts...))
	dialer := &net.Dialer{Timeout: connectTimeout}
	var lastErr error
	for _, host := range hosts {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		conn, err := dialer.DialContext(ctx, "tcp",
			net.JoinHostPort(host, strconv.Itoa(target.Port)))
		if err == nil {
			if err := authenticateEndpoint(conn, target.EndpointToken); err != nil {
				_ = conn.Close()
				lastErr = err
				continue
			}
			return conn, nil
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = errors.New("no reachable address")
	}
	return nil, lastErr
}

func authenticateEndpoint(conn net.Conn, token string) error {
	if token == "" {
		return nil
	}
	if len(token) > 256 {
		return errors.New("transport endpoint token is invalid")
	}
	for _, value := range []byte(token) {
		if value < 0x21 || value > 0x7e {
			return errors.New("transport endpoint token is invalid")
		}
	}
	if _, err := conn.Write(append([]byte("IKAT"), token...)); err != nil {
		return fmt.Errorf("authenticate transport endpoint: %w", err)
	}
	return nil
}

func sendOnce(ctx context.Context, target SendTarget, source sourceFactory,
	header *TransferHeader, cfg SenderConfig) error {
	conn, err := dialTarget(ctx, target)
	if err != nil {
		return err
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	_ = conn.SetDeadline(time.Now().Add(sendIOTimeout))

	headerRaw, err := encodeHeaderJSON(header)
	if err != nil {
		return err
	}
	frame := append([]byte(whppMagicStr),
		binary.BigEndian.AppendUint32(nil, uint32(len(headerRaw)))...)
	if _, err := conn.Write(append(frame, headerRaw...)); err != nil {
		return err
	}

	marker, err := readExact(conn, 1)
	if err != nil {
		return fmt.Errorf("接收方未返回 WHPP v3 续传状态: %w", err)
	}
	if marker[0] == ackFail {
		return fmt.Errorf("%w: 接收方拒绝了传输", ErrReceiverRejected)
	}
	if marker[0] != resume {
		return errors.New("接收方未返回 WHPP v3 续传状态")
	}
	offsetRaw, err := readExact(conn, 8)
	if err != nil {
		return err
	}
	offset := int64(binary.BigEndian.Uint64(offsetRaw))
	if offset < 0 || offset > header.PlainSize {
		return errors.New("接收方续传偏移非法")
	}
	nonce, err := readExact(conn, nonceSize)
	if err != nil {
		return err
	}
	receiverInstanceRaw, err := readExact(conn, 32)
	if err != nil {
		return err
	}
	receiverInstanceID := strings.ToLower(string(receiverInstanceRaw))
	receiverPublic, err := readSizedField(conn)
	if err != nil {
		return err
	}
	receiverSignature, err := readSizedField(conn)
	if err != nil {
		return err
	}
	receiverFingerprint, err := PublicFingerprint(string(receiverPublic))
	if err != nil {
		return errors.New("接收设备身份响应无效")
	}
	pinned := strings.ToLower(target.InstanceID)
	if !ValidInstanceID(receiverInstanceID) ||
		(pinned != "" && receiverInstanceID != pinned) ||
		(target.Fingerprint != "" && !hmac.Equal(
			[]byte(receiverFingerprint), []byte(target.Fingerprint))) {
		return errors.New("接收设备身份验证失败")
	}
	receiverMessage, err := ReceiverMessage(nonce, header, offset, receiverInstanceID)
	if err != nil {
		return err
	}
	if !Verify(string(receiverPublic), receiverMessage, string(receiverSignature)) {
		return errors.New("接收设备身份验证失败")
	}

	transferMessage, err := TransferMessage(nonce, header, offset)
	if err != nil {
		return err
	}
	signature, err := cfg.Identity.Sign(transferMessage)
	if err != nil {
		return err
	}
	signatureRaw := []byte(signature)
	reply := binary.BigEndian.AppendUint16(nil, uint16(len(signatureRaw)))
	if _, err := conn.Write(append(reply, signatureRaw...)); err != nil {
		return err
	}

	remaining := header.PlainSize - offset
	wireSize := remaining
	if header.Encrypted && remaining > 0 {
		wireSize = ChunkedWireSize(remaining)
	}
	if _, err := conn.Write(
		binary.BigEndian.AppendUint64(nil, uint64(wireSize))); err != nil {
		return err
	}
	if cfg.OnProgress != nil {
		cfg.OnProgress(header.Filename, offset, header.PlainSize)
	}
	if remaining > 0 {
		if err := sendBody(ctx, conn, source, header, offset, remaining,
			wireSize, target.UseWHE4, cfg); err != nil {
			return err
		}
	}

	_ = conn.SetDeadline(time.Now().Add(recvIdleTimeout))
	ack, err := readExact(conn, 1)
	if err != nil {
		return fmt.Errorf("未收到接收成功回执: %w", err)
	}
	if ack[0] == ackFail {
		return fmt.Errorf("%w: 接收方校验或落盘失败", ErrReceiverRejected)
	}
	if ack[0] != ackOK {
		return errors.New("未收到接收成功回执")
	}
	receivedDigest, err := readExact(conn, 32)
	if err != nil {
		return err
	}
	expected, _ := hex.DecodeString(header.SHA256)
	if !hmac.Equal(receivedDigest, expected) {
		return errors.New("接收方 SHA-256 回执不一致")
	}
	return nil
}

// encodeHeaderJSON marshals without HTML escaping so filenames survive
// byte-for-byte (Python json.dumps does not escape < > &).
func encodeHeaderJSON(header *TransferHeader) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(header); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buffer.Bytes(), "\n"), nil
}

func readSizedField(conn net.Conn) ([]byte, error) {
	sizeRaw, err := readExact(conn, 2)
	if err != nil {
		return nil, err
	}
	size := binary.BigEndian.Uint16(sizeRaw)
	if size == 0 || size > maxIdentityBytes {
		return nil, errors.New("接收设备身份响应不完整")
	}
	return readExact(conn, int(size))
}

func sendBody(ctx context.Context, conn net.Conn, source sourceFactory,
	header *TransferHeader, offset, remaining, wireSize int64,
	useWHE4 bool, cfg SenderConfig) error {
	reader, err := source(offset)
	if err != nil {
		return err
	}
	defer reader.Close()
	refreshDeadline := func() { _ = conn.SetDeadline(time.Now().Add(sendIOTimeout)) }
	progress := func(done int64) {
		if cfg.OnProgress != nil {
			cfg.OnProgress(header.Filename, done, header.PlainSize)
		}
	}
	if header.Encrypted {
		encryptor, err := NewChunkedEncryptor(cfg.Secret, useWHE4)
		if err != nil {
			return err
		}
		refreshDeadline()
		sent := int64(0)
		if _, err := conn.Write(encryptor.Header()); err != nil {
			return err
		}
		sent += 32
		buffer := make([]byte, ChunkSize)
		var plainDone int64
		for plainDone < remaining {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			chunk := buffer
			if left := remaining - plainDone; left < int64(len(chunk)) {
				chunk = chunk[:left]
			}
			read, err := io.ReadFull(reader, chunk)
			if err != nil {
				return fmt.Errorf("发送源数据不完整: %w", err)
			}
			frame := encryptor.EncryptChunk(chunk[:read])
			refreshDeadline()
			if _, err := conn.Write(frame); err != nil {
				return err
			}
			sent += int64(len(frame))
			plainDone += int64(read)
			progress(offset + plainDone)
		}
		if sent != wireSize {
			return errors.New("加密发送大小不一致")
		}
		return nil
	}
	buffer := make([]byte, recvBuffer)
	var done int64
	for done < remaining {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		chunk := buffer
		if left := remaining - done; left < int64(len(chunk)) {
			chunk = chunk[:left]
		}
		read, err := io.ReadFull(reader, chunk)
		if err != nil {
			return fmt.Errorf("发送源数据不完整: %w", err)
		}
		refreshDeadline()
		if _, err := conn.Write(chunk[:read]); err != nil {
			return err
		}
		done += int64(read)
		progress(offset + done)
	}
	return nil
}
