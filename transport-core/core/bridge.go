package core

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"sync"
	"time"

	"github.com/hashicorp/yamux"
)

type streamBridge struct {
	listener net.Listener
	session  *yamux.Session
	token    string
	cancel   context.CancelFunc
	wg       sync.WaitGroup
}

func newSendingBridge(parent context.Context, conn net.Conn) (*streamBridge, error) {
	ctx, cancel := context.WithCancel(parent)
	mux, err := yamux.Client(conn, nil)
	if err != nil {
		cancel()
		_ = conn.Close()
		return nil, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		cancel()
		_ = mux.Close()
		return nil, err
	}
	bridge := &streamBridge{listener: listener, session: mux, cancel: cancel,
		token: newCapabilityToken()}
	bridge.wg.Add(1)
	go bridge.acceptLocal(ctx)
	bridge.closeWhenDone(ctx)
	return bridge, nil
}

func newReceivingBridge(parent context.Context, conn net.Conn, target, targetToken string) (*streamBridge, error) {
	ctx, cancel := context.WithCancel(parent)
	mux, err := yamux.Server(conn, nil)
	if err != nil {
		cancel()
		_ = conn.Close()
		return nil, err
	}
	bridge := &streamBridge{session: mux, cancel: cancel}
	bridge.wg.Add(1)
	go bridge.acceptRemote(ctx, target, targetToken)
	bridge.closeWhenDone(ctx)
	return bridge, nil
}

func (b *streamBridge) closeWhenDone(ctx context.Context) {
	go func() {
		<-ctx.Done()
		if b.listener != nil {
			_ = b.listener.Close()
		}
		_ = b.session.Close()
	}()
}

func (b *streamBridge) Addr() string {
	if b.listener == nil {
		return ""
	}
	return b.listener.Addr().String()
}

func (b *streamBridge) Token() string { return b.token }

func (b *streamBridge) Close() error {
	b.cancel()
	if b.listener != nil {
		_ = b.listener.Close()
	}
	err := b.session.Close()
	b.wg.Wait()
	return err
}

func (b *streamBridge) acceptLocal(ctx context.Context) {
	defer b.wg.Done()
	for {
		local, err := b.listener.Accept()
		if err != nil {
			return
		}
		if !authenticateLoopback(local, b.token) {
			_ = local.Close()
			continue
		}
		remote, err := b.session.Open()
		if err != nil {
			_ = local.Close()
			return
		}
		b.wg.Add(1)
		go func() {
			defer b.wg.Done()
			proxyConn(ctx, local, remote)
		}()
	}
}

func (b *streamBridge) acceptRemote(ctx context.Context, target, targetToken string) {
	defer b.wg.Done()
	for {
		remote, err := b.session.Accept()
		if err != nil {
			return
		}
		local, err := net.Dial("tcp", target)
		if err != nil {
			_ = remote.Close()
			continue
		}
		if _, err := local.Write([]byte("IKCI" + targetToken)); err != nil {
			_ = local.Close()
			_ = remote.Close()
			continue
		}
		b.wg.Add(1)
		go func() {
			defer b.wg.Done()
			proxyConn(ctx, local, remote)
		}()
	}
}

func proxyConn(ctx context.Context, left, right net.Conn) {
	defer left.Close()
	defer right.Close()
	done := make(chan struct{}, 2)
	copyOne := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if tcp, ok := dst.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		done <- struct{}{}
	}
	go copyOne(left, right)
	go copyOne(right, left)
	select {
	case <-ctx.Done():
	case <-done:
		<-done
	}
}

var errSessionType = errors.New("invalid session type")

func newCapabilityToken() string {
	value := make([]byte, 24)
	if _, err := rand.Read(value); err != nil {
		panic("crypto/rand failed: " + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(value)
}

func authenticateLoopback(conn net.Conn, token string) bool {
	if token == "" {
		return false
	}
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	header := make([]byte, 4+len(token))
	_, err := io.ReadFull(conn, header)
	_ = conn.SetReadDeadline(time.Time{})
	if err != nil || string(header[:4]) != "IKAT" {
		return false
	}
	return subtle.ConstantTimeCompare(header[4:], []byte(token)) == 1
}
