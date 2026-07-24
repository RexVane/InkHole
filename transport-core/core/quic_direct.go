package core

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"strconv"
	"time"

	"github.com/pion/stun/v3"
	"github.com/quic-go/quic-go"
)

// QUIC 直连：SSH 中继模式的提速升级。两端通过中继上的一条信令流(IKQ1)
// 交换 UDP 候选地址与证书指纹，随后同时向对方候选发包在双方 NAT 上打洞，
// instanceID 大的一方发起 QUIC 握手。成功后数据面切换到直连，速度不再受
// 中继 VPS 带宽限制；失败进入冷却，期间继续走 SSH 中继，行为完全不变。
// 旧版对端把 IKQ1 当普通数据转发给应用层导致解析断开，发起方视作失败
// 冷却——无协议破坏。

const (
	directMagic          = "IKQ1"
	directVersion        = 1
	directALPN           = "inkhole-direct-1"
	directPunchDuration  = 1500 * time.Millisecond
	directPunchInterval  = 60 * time.Millisecond
	directDialTimeout    = 6 * time.Second
	directOpenTimeout    = 3 * time.Second
	directSignalTimeout  = 12 * time.Second
	directStunTimeout    = 2500 * time.Millisecond
	directCooldown       = 5 * time.Minute
	directIdleTimeout    = 60 * time.Second
	directKeepAlive      = 15 * time.Second
	directMaxSignalBytes = 8 * 1024
	directMaxCandidates  = 8
)

// 大陆与海外混合候选，命中一个即止。
var directSTUNServers = []string{
	"stun.miwifi.com:3478",
	"stun.qq.com:3478",
	"stun.cloudflare.com:3478",
	"stun.l.google.com:19302",
}

type directOffer struct {
	Version    int      `json:"version"`
	InstanceID string   `json:"instance_id"`
	Candidates []string `json:"candidates"`
	CertFP     string   `json:"cert_fp"`
}

type directAnswer struct {
	Accepted   bool     `json:"accepted"`
	Reason     string   `json:"reason,omitempty"`
	Candidates []string `json:"candidates,omitempty"`
	CertFP     string   `json:"cert_fp,omitempty"`
}

// quicLink 拥有连接与其底层 Transport(UDP socket)；两者必须一起关闭。
type quicLink struct {
	conn *quic.Conn
	tr   *quic.Transport
}

func (l *quicLink) close(reason string) {
	_ = l.conn.CloseWithError(0, reason)
	_ = l.tr.Close()
}

func directCertFingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:])
}

func generateDirectCert() (tls.Certificate, string, error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	if err != nil {
		return tls.Certificate{}, "", err
	}
	template := x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: "inkhole-direct"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(10 * 365 * 24 * time.Hour),
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, pub, priv)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	cert := tls.Certificate{Certificate: [][]byte{der}, PrivateKey: priv}
	return cert, directCertFingerprint(der), nil
}

// directTLS 用信令里声明的指纹做双向校验：证书本身自签，信任来自
// 已经过 Noise 加密与身份验证的信令通道。
func directTLS(cert tls.Certificate, expectedFP string, server bool) *tls.Config {
	conf := &tls.Config{
		Certificates:       []tls.Certificate{cert},
		NextProtos:         []string{directALPN},
		MinVersion:         tls.VersionTLS13,
		InsecureSkipVerify: true,
		VerifyPeerCertificate: func(rawCerts [][]byte, _ [][]*x509.Certificate) error {
			if len(rawCerts) == 0 {
				return errors.New("no peer certificate")
			}
			if directCertFingerprint(rawCerts[0]) != expectedFP {
				return errors.New("peer certificate fingerprint mismatch")
			}
			return nil
		},
	}
	if server {
		conf.ClientAuth = tls.RequireAnyClientCert
	}
	return conf
}

func directQUICConfig() *quic.Config {
	return &quic.Config{
		MaxIdleTimeout:  directIdleTimeout,
		KeepAlivePeriod: directKeepAlive,
	}
}

// directPublicEndpoint 从传输将要使用的同一个 UDP socket 发 STUN 查询，
// 拿到的公网映射端口才与后续 QUIC 流量一致。
func directPublicEndpoint(sock *net.UDPConn) (string, bool) {
	req := stun.MustBuild(stun.TransactionID, stun.BindingRequest)
	buf := make([]byte, 1500)
	defer func() { _ = sock.SetReadDeadline(time.Time{}) }()
	for _, server := range directSTUNServers {
		addr, err := net.ResolveUDPAddr("udp4", server)
		if err != nil {
			continue
		}
		if _, err := sock.WriteToUDP(req.Raw, addr); err != nil {
			continue
		}
		deadline := time.Now().Add(directStunTimeout)
		_ = sock.SetReadDeadline(deadline)
		for time.Now().Before(deadline) {
			n, from, err := sock.ReadFromUDP(buf)
			if err != nil {
				break
			}
			if from == nil || !from.IP.Equal(addr.IP) || from.Port != addr.Port {
				continue
			}
			msg := &stun.Message{Raw: append([]byte(nil), buf[:n]...)}
			if err := msg.Decode(); err != nil {
				continue
			}
			var mapped stun.XORMappedAddress
			if err := mapped.GetFrom(msg); err != nil {
				continue
			}
			ip := mapped.IP.To4()
			if ip == nil {
				continue
			}
			return net.JoinHostPort(ip.String(), strconv.Itoa(mapped.Port)), true
		}
	}
	return "", false
}

func directLocalCandidates(sock *net.UDPConn) []string {
	local, ok := sock.LocalAddr().(*net.UDPAddr)
	if !ok || local == nil {
		return nil
	}
	interfaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []string
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipNet.IP.To4()
			if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
				continue
			}
			out = append(out, net.JoinHostPort(ip.String(), strconv.Itoa(local.Port)))
			if len(out) >= directMaxCandidates {
				return out
			}
		}
	}
	return out
}

func directResolveCandidates(candidates []string) []*net.UDPAddr {
	seen := make(map[string]bool, len(candidates))
	var out []*net.UDPAddr
	for _, candidate := range candidates {
		if len(out) >= directMaxCandidates {
			break
		}
		addr, err := net.ResolveUDPAddr("udp4", candidate)
		if err != nil || addr.IP == nil || addr.Port == 0 {
			continue
		}
		if addr.IP.IsLoopback() || addr.IP.IsUnspecified() {
			continue
		}
		key := addr.String()
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, addr)
	}
	return out
}

// directPunch 在固定时长内向对方全部候选互发小包：去程在本方 NAT 上开洞，
// 对方的来包被读掉即可。结束后 socket 交给 QUIC Transport 接管。
func directPunch(sock *net.UDPConn, targets []*net.UDPAddr, duration time.Duration) {
	payload := []byte(directMagic)
	buf := make([]byte, 128)
	deadline := time.Now().Add(duration)
	defer func() { _ = sock.SetReadDeadline(time.Time{}) }()
	for time.Now().Before(deadline) {
		for _, target := range targets {
			_, _ = sock.WriteToUDP(payload, target)
		}
		next := time.Now().Add(directPunchInterval)
		if next.After(deadline) {
			next = deadline
		}
		_ = sock.SetReadDeadline(next)
		for {
			if _, _, err := sock.ReadFromUDP(buf); err != nil {
				break
			}
		}
	}
}

func (s *sshListenerSession) directEstablish(
	sock *net.UDPConn, isClient bool, remoteFP string,
	cert tls.Certificate, targets []*net.UDPAddr,
) (*quicLink, error) {
	tr := &quic.Transport{Conn: sock}
	fail := func(err error) (*quicLink, error) {
		_ = tr.Close()
		return nil, err
	}
	if isClient {
		ctx, cancel := context.WithTimeout(s.ctx, directDialTimeout)
		defer cancel()
		var lastErr error
		for _, target := range targets {
			attempt, attemptCancel := context.WithTimeout(ctx, directDialTimeout/2)
			conn, err := tr.Dial(attempt, target,
				directTLS(cert, remoteFP, false), directQUICConfig())
			attemptCancel()
			if err == nil {
				return &quicLink{conn: conn, tr: tr}, nil
			}
			lastErr = err
			if ctx.Err() != nil {
				break
			}
		}
		if lastErr == nil {
			lastErr = errors.New("no direct candidates")
		}
		return fail(lastErr)
	}
	listener, err := tr.Listen(directTLS(cert, remoteFP, true), directQUICConfig())
	if err != nil {
		return fail(err)
	}
	ctx, cancel := context.WithTimeout(s.ctx, directDialTimeout+directPunchDuration)
	defer cancel()
	conn, err := listener.Accept(ctx)
	if err != nil {
		_ = listener.Close()
		return fail(err)
	}
	return &quicLink{conn: conn, tr: tr}, nil
}

func (s *sshListenerSession) ensureDirectState() {
	if s.directConns == nil {
		s.directConns = make(map[string]*quicLink)
	}
	if s.directPending == nil {
		s.directPending = make(map[string]bool)
	}
	if s.directCooldown == nil {
		s.directCooldown = make(map[string]time.Time)
	}
}

func (s *sshListenerSession) directIdentity() (tls.Certificate, string, error) {
	s.directMu.Lock()
	defer s.directMu.Unlock()
	if s.directCert == nil {
		cert, fp, err := generateDirectCert()
		if err != nil {
			return tls.Certificate{}, "", err
		}
		s.directCert = &cert
		s.directFP = fp
	}
	return *s.directCert, s.directFP, nil
}

// directStream 在已有直连上开一条流；没有可用直连时返回 nil，调用方回退
// SSH 中继。失效链路顺手摘除。
func (s *sshListenerSession) directStream(instanceID string) net.Conn {
	s.directMu.Lock()
	s.ensureDirectState()
	link := s.directConns[instanceID]
	if link != nil {
		select {
		case <-link.conn.Context().Done():
			delete(s.directConns, instanceID)
			link.close("stale")
			link = nil
		default:
		}
	}
	s.directMu.Unlock()
	if link == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(s.ctx, directOpenTimeout)
	defer cancel()
	stream, err := link.conn.OpenStreamSync(ctx)
	if err != nil {
		return nil
	}
	return &quicStreamConn{
		Stream: stream,
		local:  link.conn.LocalAddr(),
		remote: link.conn.RemoteAddr(),
	}
}

// maybeDirect 是发起方的节流入口：已有直连、正在尝试或冷却中都直接返回。
func (s *sshListenerSession) maybeDirect(peer SSHPeer) {
	s.directMu.Lock()
	s.ensureDirectState()
	if link := s.directConns[peer.InstanceID]; link != nil {
		select {
		case <-link.conn.Context().Done():
			delete(s.directConns, peer.InstanceID)
			link.close("stale")
		default:
			s.directMu.Unlock()
			return
		}
	}
	if s.directPending[peer.InstanceID] ||
		time.Now().Before(s.directCooldown[peer.InstanceID]) {
		s.directMu.Unlock()
		return
	}
	s.directPending[peer.InstanceID] = true
	s.directMu.Unlock()
	go func() {
		err := s.tryDirect(peer)
		s.directMu.Lock()
		delete(s.directPending, peer.InstanceID)
		if err != nil {
			s.directCooldown[peer.InstanceID] = time.Now().Add(directCooldown)
		}
		s.directMu.Unlock()
		if err != nil && s.ctx.Err() == nil {
			s.service.emit("ssh.direct.failed", map[string]any{
				"peer_id": peer.InstanceID,
				"error":   errorString(err),
			})
		}
	}()
}

func (s *sshListenerSession) tryDirect(peer SSHPeer) error {
	cert, fp, err := s.directIdentity()
	if err != nil {
		return err
	}
	sock, err := net.ListenUDP("udp4", &net.UDPAddr{})
	if err != nil {
		return err
	}
	adopted := false
	defer func() {
		if !adopted {
			_ = sock.Close()
		}
	}()
	candidates := directLocalCandidates(sock)
	if public, ok := directPublicEndpoint(sock); ok {
		candidates = append([]string{public}, candidates...)
	}
	if len(candidates) == 0 {
		return errors.New("no local candidates")
	}

	s.mu.RLock()
	endpoint := s.endpoints[peer.InstanceID]
	s.mu.RUnlock()
	if endpoint == nil {
		return errors.New("peer endpoint is gone")
	}
	signal, err := endpoint.openRelayStream()
	if err != nil {
		return err
	}
	defer signal.Close()
	_ = signal.SetDeadline(time.Now().Add(directSignalTimeout))
	if _, err := signal.Write([]byte(directMagic)); err != nil {
		return err
	}
	offer, _ := json.Marshal(directOffer{
		Version:    directVersion,
		InstanceID: s.identity.InstanceID,
		Candidates: candidates,
		CertFP:     fp,
	})
	if err := writeFrame(signal, offer); err != nil {
		return err
	}
	answerRaw, err := readFrame(signal, directMaxSignalBytes)
	if err != nil {
		return fmt.Errorf("direct signaling failed (peer may be older): %w", err)
	}
	var answer directAnswer
	if err := json.Unmarshal(answerRaw, &answer); err != nil {
		return err
	}
	if !answer.Accepted || answer.CertFP == "" || len(answer.Candidates) == 0 {
		reason := answer.Reason
		if reason == "" {
			reason = "declined"
		}
		return errors.New("peer declined direct link: " + reason)
	}
	targets := directResolveCandidates(answer.Candidates)
	if len(targets) == 0 {
		return errors.New("no usable peer candidates")
	}

	directPunch(sock, targets, directPunchDuration)
	link, err := s.directEstablish(
		sock, s.identity.InstanceID > peer.InstanceID, answer.CertFP, cert, targets)
	if err != nil {
		return err
	}
	adopted = true
	s.adoptDirect(peer.InstanceID, link)
	return nil
}

// handleDirectSignal 是响应方：信令流已经过 Noise 身份验证，offer 声明的
// instanceID 必须与流归属一致，防止借道冒充。
func (s *sshListenerSession) handleDirectSignal(stream net.Conn, instanceID string) {
	defer stream.Close()
	_ = stream.SetDeadline(time.Now().Add(directSignalTimeout))
	decline := func(reason string) {
		raw, _ := json.Marshal(directAnswer{Accepted: false, Reason: reason})
		_ = writeFrame(stream, raw)
	}
	offerRaw, err := readFrame(stream, directMaxSignalBytes)
	if err != nil {
		return
	}
	var offer directOffer
	if err := json.Unmarshal(offerRaw, &offer); err != nil {
		decline("bad offer")
		return
	}
	if offer.Version != directVersion || offer.InstanceID != instanceID ||
		offer.CertFP == "" || len(offer.Candidates) == 0 {
		decline("unsupported offer")
		return
	}
	targets := directResolveCandidates(offer.Candidates)
	if len(targets) == 0 {
		decline("no usable candidates")
		return
	}
	s.directMu.Lock()
	s.ensureDirectState()
	if link := s.directConns[instanceID]; link != nil {
		select {
		case <-link.conn.Context().Done():
			delete(s.directConns, instanceID)
			link.close("stale")
		default:
			s.directMu.Unlock()
			decline("already connected")
			return
		}
	}
	if s.directPending[instanceID] {
		s.directMu.Unlock()
		decline("busy")
		return
	}
	s.directPending[instanceID] = true
	s.directMu.Unlock()
	defer func() {
		s.directMu.Lock()
		delete(s.directPending, instanceID)
		s.directMu.Unlock()
	}()

	cert, fp, err := s.directIdentity()
	if err != nil {
		decline("internal error")
		return
	}
	sock, err := net.ListenUDP("udp4", &net.UDPAddr{})
	if err != nil {
		decline("no udp socket")
		return
	}
	adopted := false
	defer func() {
		if !adopted {
			_ = sock.Close()
		}
	}()
	candidates := directLocalCandidates(sock)
	if public, ok := directPublicEndpoint(sock); ok {
		candidates = append([]string{public}, candidates...)
	}
	if len(candidates) == 0 {
		decline("no local candidates")
		return
	}
	answer, _ := json.Marshal(directAnswer{
		Accepted:   true,
		Candidates: candidates,
		CertFP:     fp,
	})
	if err := writeFrame(stream, answer); err != nil {
		return
	}
	directPunch(sock, targets, directPunchDuration)
	link, err := s.directEstablish(
		sock, s.identity.InstanceID > offer.InstanceID, offer.CertFP, cert, targets)
	if err != nil {
		return
	}
	adopted = true
	s.adoptDirect(instanceID, link)
}

func (s *sshListenerSession) adoptDirect(instanceID string, link *quicLink) {
	s.directMu.Lock()
	s.ensureDirectState()
	if old := s.directConns[instanceID]; old != nil {
		old.close("replaced")
	}
	s.directConns[instanceID] = link
	s.directMu.Unlock()
	s.service.emit("ssh.direct.up", map[string]any{"peer_id": instanceID})
	s.wg.Add(1)
	go s.serveDirect(instanceID, link)
}

// serveDirect 接收对端在直连上打开的流并转发给本地应用；连接退出时摘除
// 链路并广播状态，之后的传输自动回到 SSH 中继并允许重新打洞。
func (s *sshListenerSession) serveDirect(instanceID string, link *quicLink) {
	defer s.wg.Done()
	for {
		stream, err := link.conn.AcceptStream(s.ctx)
		if err != nil {
			s.directMu.Lock()
			if s.directConns[instanceID] == link {
				delete(s.directConns, instanceID)
			}
			s.directMu.Unlock()
			link.close("closed")
			if s.ctx.Err() == nil {
				s.service.emit("ssh.direct.down", map[string]any{
					"peer_id": instanceID,
					"error":   errorString(err),
				})
			}
			return
		}
		s.wg.Add(1)
		go func(stream *quic.Stream) {
			defer s.wg.Done()
			local, err := net.Dial("tcp", s.target)
			if err != nil {
				stream.CancelRead(0)
				_ = stream.Close()
				return
			}
			if _, err := local.Write([]byte("IKCI" + s.targetToken)); err != nil {
				_ = local.Close()
				stream.CancelRead(0)
				_ = stream.Close()
				return
			}
			proxyConn(s.ctx, local, &quicStreamConn{
				Stream: stream,
				local:  link.conn.LocalAddr(),
				remote: link.conn.RemoteAddr(),
			})
		}(stream)
	}
}

func (s *sshListenerSession) closeDirect() {
	s.directMu.Lock()
	links := make([]*quicLink, 0, len(s.directConns))
	for _, link := range s.directConns {
		links = append(links, link)
	}
	s.directConns = make(map[string]*quicLink)
	s.directMu.Unlock()
	for _, link := range links {
		link.close("session closed")
	}
}

// quicStreamConn 把 QUIC 流包成 net.Conn 供 proxyConn 使用。Close 同时
// 终止读向，避免半关流长期占住对端资源。
type quicStreamConn struct {
	*quic.Stream
	local  net.Addr
	remote net.Addr
}

func (c *quicStreamConn) LocalAddr() net.Addr  { return c.local }
func (c *quicStreamConn) RemoteAddr() net.Addr { return c.remote }

func (c *quicStreamConn) Close() error {
	c.Stream.CancelRead(0)
	return c.Stream.Close()
}

var _ io.ReadWriteCloser = (*quicStreamConn)(nil)
