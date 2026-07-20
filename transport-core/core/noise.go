package core

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"github.com/flynn/noise"
)

const maxSecureRecord = 64 * 1024

var noiseSuite = noise.NewCipherSuite(noise.DH25519, noise.CipherChaChaPoly, noise.HashBLAKE2s)

func generateNoiseKey() (noise.DHKey, error) {
	return noise.DH25519.GenerateKeypair(rand.Reader)
}

func decodeNoiseKey(encoded string) (noise.DHKey, error) {
	privateKey, err := base64.RawStdEncoding.DecodeString(encoded)
	if err != nil || len(privateKey) != 32 {
		return noise.DHKey{}, errors.New("invalid Noise private key")
	}
	publicKey := generatePublicKey(privateKey)
	if len(publicKey) != 32 {
		return noise.DHKey{}, errors.New("failed to derive Noise public key")
	}
	return noise.DHKey{Private: privateKey, Public: publicKey}, nil
}

func encodeNoisePrivate(key noise.DHKey) string {
	return base64.RawStdEncoding.EncodeToString(key.Private)
}

func encodeNoisePublic(key []byte) string {
	return base64.RawStdEncoding.EncodeToString(key)
}

func decodeNoisePublic(encoded string) ([]byte, error) {
	key, err := base64.RawStdEncoding.DecodeString(encoded)
	if err != nil || len(key) != 32 {
		return nil, errors.New("invalid Noise public key")
	}
	return key, nil
}

func noiseInitiator(conn net.Conn, key noise.DHKey, peerPublic []byte, payload []byte,
	encrypted bool) (net.Conn, error) {
	handshake, err := noise.NewHandshakeState(noise.Config{
		CipherSuite:   noiseSuite,
		Random:        rand.Reader,
		Pattern:       noise.HandshakeIK,
		Initiator:     true,
		StaticKeypair: key,
		PeerStatic:    peerPublic,
	})
	if err != nil {
		return nil, err
	}
	message, _, _, err := handshake.WriteMessage(nil, payload)
	if err != nil {
		return nil, err
	}
	if err := writeFrame(conn, message); err != nil {
		return nil, err
	}
	reply, err := readFrame(conn, 128*1024)
	if err != nil {
		return nil, err
	}
	_, send, receive, err := handshake.ReadMessage(nil, reply)
	if err != nil {
		return nil, err
	}
	if send == nil || receive == nil {
		return nil, errors.New("Noise IK handshake did not finish")
	}
	if !encrypted {
		return conn, nil
	}
	return &noiseConn{Conn: conn, send: send, receive: receive}, nil
}

func noiseResponder(conn net.Conn, key noise.DHKey) (net.Conn, []byte, []byte,
	*noise.CipherState, *noise.CipherState, error) {
	handshake, err := noise.NewHandshakeState(noise.Config{
		CipherSuite:   noiseSuite,
		Random:        rand.Reader,
		Pattern:       noise.HandshakeIK,
		Initiator:     false,
		StaticKeypair: key,
	})
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	message, err := readFrame(conn, 128*1024)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	payload, _, _, err := handshake.ReadMessage(nil, message)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	reply, receive, send, err := handshake.WriteMessage(nil, nil)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	if receive == nil || send == nil {
		return nil, nil, nil, nil, nil, errors.New("Noise IK handshake did not finish")
	}
	if err := writeFrame(conn, reply); err != nil {
		return nil, nil, nil, nil, nil, err
	}
	peerStatic := append([]byte(nil), handshake.PeerStatic()...)
	return conn, peerStatic, payload, send, receive, nil
}

func finishNoiseResponder(conn net.Conn, send, receive *noise.CipherState,
	encrypted bool) net.Conn {
	if !encrypted {
		return conn
	}
	return &noiseConn{Conn: conn, send: send, receive: receive}
}

type noiseConn struct {
	net.Conn
	send       *noise.CipherState
	receive    *noise.CipherState
	readMu     sync.Mutex
	writeMu    sync.Mutex
	readBuffer []byte
}

func (c *noiseConn) Read(p []byte) (int, error) {
	c.readMu.Lock()
	defer c.readMu.Unlock()
	if len(c.readBuffer) == 0 {
		ciphertext, err := readFrame(c.Conn, maxSecureRecord+1024)
		if err != nil {
			return 0, err
		}
		plain, err := c.receive.Decrypt(nil, nil, ciphertext)
		if err != nil {
			return 0, err
		}
		c.readBuffer = plain
	}
	n := copy(p, c.readBuffer)
	c.readBuffer = c.readBuffer[n:]
	return n, nil
}

func (c *noiseConn) Write(p []byte) (int, error) {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	written := 0
	for len(p) > 0 {
		n := len(p)
		if n > maxSecureRecord {
			n = maxSecureRecord
		}
		ciphertext, err := c.send.Encrypt(nil, nil, p[:n])
		if err != nil {
			return written, err
		}
		if err := writeFrame(c.Conn, ciphertext); err != nil {
			return written, err
		}
		written += n
		p = p[n:]
	}
	return written, nil
}

func writeFrame(w io.Writer, payload []byte) error {
	if len(payload) > 4*1024*1024 {
		return errors.New("frame is too large")
	}
	header := make([]byte, 4)
	binary.BigEndian.PutUint32(header, uint32(len(payload)))
	if _, err := w.Write(header); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

func readFrame(r io.Reader, limit int) ([]byte, error) {
	header := make([]byte, 4)
	if _, err := io.ReadFull(r, header); err != nil {
		return nil, err
	}
	size := int(binary.BigEndian.Uint32(header))
	if size < 0 || size > limit {
		return nil, fmt.Errorf("invalid frame size: %d", size)
	}
	payload := make([]byte, size)
	_, err := io.ReadFull(r, payload)
	return payload, err
}

func setHandshakeDeadline(conn net.Conn) func() {
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
	return func() { _ = conn.SetDeadline(time.Time{}) }
}
