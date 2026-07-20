package wormhole

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"github.com/psanford/wormhole-william/internal/crypto"
	"github.com/psanford/wormhole-william/rendezvous"
	"github.com/psanford/wormhole-william/wordlist"
	"golang.org/x/crypto/nacl/secretbox"
)

// TunnelResult is delivered when the receiver accepts or rendezvous fails.
type TunnelResult struct {
	Conn  net.Conn
	Error error
}

// TunnelOffer carries application metadata and defers the transit connection
// until the user explicitly accepts it.
type TunnelOffer struct {
	Metadata string
	mu       sync.Mutex
	used     bool
	accept   func() (net.Conn, error)
	reject   func() error
}

func (o *TunnelOffer) Accept() (net.Conn, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.used {
		return nil, errors.New("tunnel offer already handled")
	}
	o.used = true
	return o.accept()
}

func (o *TunnelOffer) Reject() error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.used {
		return nil
	}
	o.used = true
	return o.reject()
}

// OpenTunnel exposes the mutually authenticated transit stream instead of
// applying Magic Wormhole's built-in file/ZIP protocol to it.
func (c *Client) OpenTunnel(ctx context.Context, metadata string) (string, <-chan TunnelResult, error) {
	if err := c.validateRelayAddr(); err != nil {
		return "", nil, fmt.Errorf("invalid TransitRelayAddress: %s", err)
	}
	sideID := crypto.RandSideID()
	appID := c.appID()
	options, err := c.rendezvousOptions()
	if err != nil {
		return "", nil, err
	}
	rc := rendezvous.NewClient(c.url(), sideID, appID, options...)
	connectCtx, connectCancel := context.WithTimeout(ctx, 20*time.Second)
	defer connectCancel()
	if _, err := rc.Connect(connectCtx); err != nil {
		return "", nil, err
	}
	nameplate, err := rc.CreateMailbox(connectCtx)
	if err != nil {
		_ = rc.Close(context.Background(), rendezvous.Errory)
		return "", nil, err
	}
	code := nameplate + "-" + wordlist.ChooseWords(c.wordCount())
	result := make(chan TunnelResult, 1)
	go func() {
		mood := rendezvous.Errory
		defer func() {
			_ = rc.Close(context.Background(), mood)
			close(result)
		}()
		fail := func(err error) { result <- TunnelResult{Error: err} }
		protocol := newClientProtocol(ctx, rc, sideID, appID)
		if err := tunnelPAKE(ctx, c, protocol, code); err != nil {
			fail(err)
			return
		}
		transitKey := deriveTransitKey(protocol.sharedKey, appID)
		transport := newFileTransport(transitKey, appID, c.relayAddr(), c.ProxyURL)
		if err := transport.listen(); err != nil {
			fail(err)
			return
		}
		// Transit is a fallback. A blocked relay must not prevent two peers
		// that have usable direct hints from completing the session.
		_ = transport.listenRelay(ctx)
		transit, err := transport.makeTransitMsg()
		if err != nil {
			fail(err)
			return
		}
		if err := protocol.WriteAppData(ctx, &genericMessage{Transit: transit}); err != nil {
			fail(err)
			return
		}
		if err := protocol.WriteAppData(ctx, &genericMessage{
			Offer: &offerMsg{Message: &metadata},
		}); err != nil {
			fail(err)
			return
		}
		collector, err := protocol.Collect()
		if err != nil {
			fail(err)
			return
		}
		defer collector.close()
		var answer answerMsg
		if err := collector.waitFor(&answer); err != nil {
			fail(err)
			return
		}
		if answer.MessageAck != "ok" {
			fail(errors.New("receiver did not accept tunnel"))
			return
		}
		conn, err := transport.acceptConnection(ctx)
		if err != nil {
			fail(err)
			return
		}
		mood = rendezvous.Happy
		result <- TunnelResult{Conn: newTunnelConn(conn, transitKey,
			"transit_record_receiver_key", "transit_record_sender_key")}
	}()
	return code, result, nil
}

// ReceiveTunnel authenticates the short code and returns the sender metadata.
func (c *Client) ReceiveTunnel(ctx context.Context, code string) (*TunnelOffer, error) {
	if err := c.validateRelayAddr(); err != nil {
		return nil, fmt.Errorf("invalid TransitRelayAddress: %s", err)
	}
	sideID := crypto.RandSideID()
	appID := c.appID()
	options, err := c.rendezvousOptions()
	if err != nil {
		return nil, err
	}
	rc := rendezvous.NewClient(c.url(), sideID, appID, options...)
	fail := func(err error) (*TunnelOffer, error) {
		_ = rc.Close(context.Background(), rendezvous.Errory)
		return nil, err
	}
	connectCtx, connectCancel := context.WithTimeout(ctx, 20*time.Second)
	defer connectCancel()
	if _, err := rc.Connect(connectCtx); err != nil {
		return fail(err)
	}
	nameplate, err := nameplateFromCode(code)
	if err != nil {
		return fail(err)
	}
	if err := rc.AttachMailbox(connectCtx, nameplate); err != nil {
		return fail(err)
	}
	protocol := newClientProtocol(ctx, rc, sideID, appID)
	if err := tunnelPAKE(ctx, c, protocol, code); err != nil {
		return fail(err)
	}
	collector, err := protocol.Collect(collectOffer, collectTransit)
	if err != nil {
		return fail(err)
	}
	var offer offerMsg
	if err := collector.waitFor(&offer); err != nil {
		collector.close()
		return fail(err)
	}
	if offer.Message == nil {
		collector.close()
		return fail(errors.New("tunnel offer has no metadata"))
	}
	var otherTransit transitMsg
	if err := collector.waitFor(&otherTransit); err != nil {
		collector.close()
		return fail(err)
	}
	collector.close()

	transitKey := deriveTransitKey(protocol.sharedKey, appID)
	transport := newFileTransport(transitKey, appID, c.relayAddr(), c.ProxyURL)
	ours, err := transport.makeTransitMsg()
	if err != nil {
		return fail(err)
	}
	if err := protocol.WriteAppData(ctx, &genericMessage{Transit: ours}); err != nil {
		return fail(err)
	}
	return &TunnelOffer{
		Metadata: *offer.Message,
		accept: func() (net.Conn, error) {
			answer := &genericMessage{Answer: &answerMsg{MessageAck: "ok"}}
			if err := protocol.WriteAppData(context.Background(), answer); err != nil {
				return failTunnelConn(rc, err)
			}
			conn, err := transport.connectDirect(&otherTransit)
			if err != nil {
				return failTunnelConn(rc, err)
			}
			if conn == nil {
				conn, err = transport.connectViaRelay(&otherTransit)
				if err != nil {
					return failTunnelConn(rc, err)
				}
			}
			if conn == nil {
				return failTunnelConn(rc, errors.New("failed to establish transit connection"))
			}
			_ = rc.Close(context.Background(), rendezvous.Happy)
			return newTunnelConn(conn, transitKey,
				"transit_record_sender_key", "transit_record_receiver_key"), nil
		},
		reject: func() error {
			reason := "transfer rejected"
			err := protocol.WriteAppData(context.Background(), &genericMessage{Error: &reason})
			_ = rc.Close(context.Background(), rendezvous.Errory)
			return err
		},
	}, nil
}

func tunnelPAKE(ctx context.Context, client *Client, protocol *clientProtocol, code string) error {
	if err := protocol.WritePake(ctx, code); err != nil {
		return err
	}
	if err := protocol.ReadPake(ctx); err != nil {
		return err
	}
	if err := protocol.WriteVersion(ctx); err != nil {
		return err
	}
	if _, err := protocol.ReadVersion(); err != nil {
		return err
	}
	if client.VerifierOk != nil {
		verifier, err := protocol.Verifier()
		if err != nil {
			return err
		}
		if !client.VerifierOk(hex.EncodeToString(verifier)) {
			return errors.New("verification rejected")
		}
	}
	return nil
}

func failTunnelConn(rc *rendezvous.Client, err error) (net.Conn, error) {
	_ = rc.Close(context.Background(), rendezvous.Errory)
	return nil, err
}

type tunnelConn struct {
	cryptor *transportCryptor
	readMu  sync.Mutex
	writeMu sync.Mutex
	buf     []byte
}

func newTunnelConn(conn net.Conn, key []byte, readPurpose, writePurpose string) net.Conn {
	return &tunnelConn{cryptor: newTransportCryptor(conn, key, readPurpose, writePurpose)}
}

func (c *tunnelConn) Read(p []byte) (int, error) {
	c.readMu.Lock()
	defer c.readMu.Unlock()
	if len(c.buf) == 0 {
		record, err := c.cryptor.readRecord()
		if err != nil {
			return 0, err
		}
		c.buf = record
	}
	n := copy(p, c.buf)
	c.buf = c.buf[n:]
	return n, nil
}

func (c *tunnelConn) Write(p []byte) (int, error) {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	written := 0
	const recordSize = (1 << 14) - secretbox.Overhead
	for len(p) > 0 {
		n := len(p)
		if n > recordSize {
			n = recordSize
		}
		if err := c.cryptor.writeRecord(p[:n]); err != nil {
			return written, err
		}
		written += n
		p = p[n:]
	}
	return written, nil
}

func (c *tunnelConn) Close() error                       { return c.cryptor.Close() }
func (c *tunnelConn) LocalAddr() net.Addr                { return c.cryptor.conn.LocalAddr() }
func (c *tunnelConn) RemoteAddr() net.Addr               { return c.cryptor.conn.RemoteAddr() }
func (c *tunnelConn) SetDeadline(t time.Time) error      { return c.cryptor.conn.SetDeadline(t) }
func (c *tunnelConn) SetReadDeadline(t time.Time) error  { return c.cryptor.conn.SetReadDeadline(t) }
func (c *tunnelConn) SetWriteDeadline(t time.Time) error { return c.cryptor.conn.SetWriteDeadline(t) }

var _ io.ReadWriteCloser = (*tunnelConn)(nil)
