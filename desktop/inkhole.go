package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	transportcore "github.com/rexvane/inkhole/transport-core/core"
	"github.com/rexvane/inkhole/transport-core/core/lan"
	"github.com/wailsapp/wails/v3/pkg/application"
)

type PeerView struct {
	InstanceID   string   `json:"instanceId"`
	Name         string   `json:"name"`
	Host         string   `json:"host"`
	Port         int      `json:"port"`
	Fingerprint  string   `json:"fingerprint"`
	Transport    string   `json:"transport"`
	Routes       []string `json:"routes"`
	Capabilities []string `json:"capabilities,omitempty"`
}

type runtimeSSHPeer struct {
	ID            string `json:"id"`
	Name          string `json:"name"`
	InstanceID    string `json:"instance_id"`
	RemotePort    int    `json:"remote_port"`
	NoisePublic   string `json:"noise_public"`
	EndToEnd      bool   `json:"end_to_end"`
	Endpoint      string `json:"endpoint"`
	EndpointToken string `json:"endpoint_token"`
}

type InkHoleService struct {
	app         *application.App
	credentials credentialStore
	petWindowMu sync.RWMutex
	petWindow   *application.WebviewWindow
	petMotion   atomic.Uint64

	mu                sync.Mutex
	cfg               Config
	configLoaded      bool
	credentialWarning string
	legacyIdentity    string
	legacySecret      string
	identity          *lan.Identity
	secret            string
	running           bool
	listener          net.Listener
	receiver          *lan.Receiver
	discovery         *lan.Discovery
	cancel            context.CancelFunc
	ctx               context.Context
	ingressToken      string
	actualPort        int
	conns             map[net.Conn]struct{}
	lanPeers          map[string]lan.Peer
	manualRuntime     map[string]PeerView
	manualStrikes     map[string]int
	manualProbeGen    uint64
	manualProbe       func(string, int, time.Duration, string) (*lan.ProbeResult, error)
	sshRuntime        map[string]runtimeSSHPeer
	// endpointWHE4 caches per-instance bridge probe outcomes for the node
	// session, so repeated batches to the same SSH peer skip the relay
	// round-trip. Cleared on Stop; a peer upgrade is picked up on restart.
	endpointWHE4 map[string]bool
	selected     string
	sends        map[string]context.CancelFunc

	transport       *transportcore.Service
	sshMu           sync.Mutex
	sshSessionID    string
	sshConnected    bool
	pendingWormhole map[string][]string

	nodeWG sync.WaitGroup
	sendWG sync.WaitGroup
}

func NewInkHoleService() *InkHoleService {
	return &InkHoleService{
		credentials:     systemCredentialStore{},
		lanPeers:        make(map[string]lan.Peer),
		manualRuntime:   make(map[string]PeerView),
		manualStrikes:   make(map[string]int),
		sshRuntime:      make(map[string]runtimeSSHPeer),
		endpointWHE4:    make(map[string]bool),
		sends:           make(map[string]context.CancelFunc),
		pendingWormhole: make(map[string][]string),
	}
}

func (s *InkHoleService) setApp(app *application.App) { s.app = app }

func (s *InkHoleService) setPetWindow(window *application.WebviewWindow) {
	s.petWindowMu.Lock()
	s.petWindow = window
	s.petWindowMu.Unlock()
}

func (s *InkHoleService) currentPetWindow() *application.WebviewWindow {
	s.petWindowMu.RLock()
	defer s.petWindowMu.RUnlock()
	return s.petWindow
}

// CancelPetMotion stops an in-progress expand or collapse animation. Dragging
// calls this before handing control to the native window manager.
func (s *InkHoleService) CancelPetMotion() {
	s.petMotion.Add(1)
}

// DragPet performs a native window drag and returns when the mouse button is
// released. performWindowDragWithEvent hands the window to the window server
// and returns immediately, so the release boundary must come from polling the
// global button state — the WebView never sees the mouseup once the native
// drag owns the event stream, and returning early made snap() fight the
// still-running drag from the start position.
func (s *InkHoleService) DragPet() error {
	window := s.currentPetWindow()
	if window == nil {
		return errors.New("桌宠窗口尚未就绪")
	}
	s.CancelPetMotion()
	window.HandleMessage("wails:drag")
	deadline := time.Now().Add(30 * time.Second)
	if dragEndPollSupported {
		for leftMouseButtonDown() && time.Now().Before(deadline) {
			time.Sleep(10 * time.Millisecond)
		}
		return nil
	}
	// 没有全局按键状态的平台退化为位置稳定启发式：窗口连续 300ms 不动
	// 即视为拖动结束。
	lastX, lastY := window.Position()
	stableSince := time.Now()
	for time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
		x, y := window.Position()
		if x != lastX || y != lastY {
			lastX, lastY = x, y
			stableSince = time.Now()
			continue
		}
		if time.Since(stableSince) >= 300*time.Millisecond {
			return nil
		}
	}
	return nil
}

// MovePetTo animates the native window position independently of WebView
// rendering. A WebView can stop requestAnimationFrame after most of a
// transparent window leaves the screen, which otherwise strands the pet
// halfway through its collapsed position.
func (s *InkHoleService) MovePetTo(x, y, durationMS int) error {
	window := s.currentPetWindow()
	if window == nil {
		return errors.New("桌宠窗口尚未就绪")
	}
	if durationMS < 0 || durationMS > 1000 {
		return errors.New("桌宠动画时长无效")
	}

	generation := s.petMotion.Add(1)
	startX, startY := window.Position()
	if durationMS == 0 || (startX == x && startY == y) {
		window.SetPosition(x, y)
		return nil
	}

	duration := time.Duration(durationMS) * time.Millisecond
	startedAt := time.Now()
	ticker := time.NewTicker(time.Second / 60)
	defer ticker.Stop()

	for {
		if s.petMotion.Load() != generation {
			return nil
		}
		elapsed := time.Since(startedAt)
		if elapsed >= duration {
			window.SetPosition(x, y)
			return nil
		}
		progress := float64(elapsed) / float64(duration)
		eased := 1 - math.Pow(1-progress, 3)
		currentX := startX + int(math.Round(float64(x-startX)*eased))
		currentY := startY + int(math.Round(float64(y-startY)*eased))
		window.SetPosition(currentX, currentY)
		<-ticker.C
	}
}

// GetPetScreenArea returns the current screen in the same logical coordinate
// space as WebviewWindow.Position. Wails alpha2.117's macOS GetScreen path
// skips ScreenManager's DPI conversion, so normalise its raw Retina values
// here instead of mixing them with logical window coordinates.
func (s *InkHoleService) GetPetScreenArea() (map[string]int, error) {
	window := s.currentPetWindow()
	if window == nil {
		return nil, errors.New("桌宠窗口尚未就绪")
	}
	screen, err := window.GetScreen()
	if err != nil {
		return nil, fmt.Errorf("无法获取桌宠所在屏幕: %w", err)
	}
	if screen == nil {
		return nil, errors.New("无法获取桌宠所在屏幕")
	}
	bounds := screen.Bounds
	if runtime.GOOS == "darwin" && screen.ScaleFactor > 0 {
		scale := float64(screen.ScaleFactor)
		bounds.X = int(math.Round(float64(bounds.X) / scale))
		bounds.Y = int(math.Round(float64(bounds.Y) / scale))
		bounds.Width = int(math.Round(float64(bounds.Width) / scale))
		bounds.Height = int(math.Round(float64(bounds.Height) / scale))
	}
	if bounds.Width <= 0 || bounds.Height <= 0 {
		return nil, errors.New("桌宠所在屏幕范围无效")
	}
	return map[string]int{
		"X": bounds.X, "Y": bounds.Y,
		"Width": bounds.Width, "Height": bounds.Height,
	}, nil
}

func (s *InkHoleService) emit(name string, data any) {
	if s.app != nil {
		s.app.Event.Emit(name, data)
	}
}

func (s *InkHoleService) GetConfig() (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.loadConfigLocked(); err != nil {
		return nil, err
	}
	hasSecret, secretErr := s.credentials.Has(transferCredential)
	if secretErr != nil {
		hasSecret = false
	}
	return map[string]any{
		"peerName":          s.cfg.PeerName,
		"instanceId":        s.cfg.InstanceID,
		"inbox":             s.cfg.Inbox,
		"hasSecret":         hasSecret || s.legacySecret != "",
		"encryptionEnabled": s.cfg.EncryptionEnabled,
		"port":              s.cfg.ListenPort,
		"actualPort":        s.actualPort,
		"running":           s.running,
		"showPet":           s.cfg.ShowPet,
		"version":           appVersion,
		"warning":           s.credentialWarning,
		"addresses":         lan.LocalIPv4s(),
	}, nil
}

func (s *InkHoleService) SaveConfig(peerName, inbox, secret string,
	clearSecret bool, port int, showPet bool, encryptionEnabled bool) error {
	s.mu.Lock()
	if err := s.loadConfigLocked(); err != nil {
		s.mu.Unlock()
		return err
	}
	peerName = strings.TrimSpace(peerName)
	if peerName == "" {
		s.mu.Unlock()
		return errors.New("设备名称不能为空")
	}
	if len([]rune(peerName)) > 40 {
		s.mu.Unlock()
		return errors.New("设备名称不能超过 40 个字符")
	}
	inbox = filepath.Clean(strings.TrimSpace(inbox))
	if inbox == "" || inbox == "." {
		s.mu.Unlock()
		return errors.New("收件箱目录不能为空")
	}
	if port < 0 || port > 65535 {
		s.mu.Unlock()
		return errors.New("监听端口必须是 0 到 65535")
	}
	if clearSecret {
		if err := s.credentials.Delete(transferCredential); err != nil {
			s.mu.Unlock()
			return fmt.Errorf("清除传输口令失败: %w", err)
		}
		s.legacySecret = ""
		encryptionEnabled = false
	} else if secret != "" {
		if err := s.credentials.Set(transferCredential, secret); err != nil {
			s.mu.Unlock()
			return fmt.Errorf("保存传输口令失败: %w", err)
		}
		s.legacySecret = ""
	}
	if encryptionEnabled && secret == "" && s.legacySecret == "" {
		stored, err := s.credentials.Has(transferCredential)
		if err != nil || !stored {
			s.mu.Unlock()
			return errors.New("启用加密前请先设置端到端口令")
		}
	}
	restart := s.running && (s.cfg.PeerName != peerName || s.cfg.Inbox != inbox ||
		s.cfg.ListenPort != port || secret != "" || clearSecret ||
		s.cfg.EncryptionEnabled != encryptionEnabled)
	s.cfg.PeerName = peerName
	s.cfg.Inbox = inbox
	s.cfg.ListenPort = port
	s.cfg.ShowPet = showPet
	s.cfg.EncryptionEnabled = encryptionEnabled
	if err := s.saveConfigLocked(); err != nil {
		s.mu.Unlock()
		return err
	}
	s.mu.Unlock()
	s.SetPetVisible(showPet)
	if restart {
		s.Stop()
		return s.Start()
	}
	return nil
}

// DisablePet is the pet context menu's "关闭桌宠": hide the widget and
// persist showPet=false, exactly like unticking the settings toggle. The
// app keeps running in the tray.
func (s *InkHoleService) DisablePet() {
	s.mu.Lock()
	if err := s.loadConfigLocked(); err == nil {
		s.cfg.ShowPet = false
		_ = s.saveConfigLocked()
	}
	s.mu.Unlock()
	s.SetPetVisible(false)
}

func (s *InkHoleService) Start() error {
	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return nil
	}
	if err := s.loadConfigLocked(); err != nil {
		s.mu.Unlock()
		return err
	}
	identity, err := s.loadIdentityLocked()
	if err != nil {
		s.mu.Unlock()
		return err
	}
	secret, err := s.activeTransferSecretLocked()
	if err != nil {
		s.mu.Unlock()
		return err
	}
	if err := os.MkdirAll(s.cfg.Inbox, 0o755); err != nil {
		s.mu.Unlock()
		return fmt.Errorf("无法创建收件箱: %w", err)
	}
	listener, err := net.Listen("tcp", net.JoinHostPort("", strconv.Itoa(s.cfg.ListenPort)))
	if err != nil {
		s.mu.Unlock()
		return fmt.Errorf("监听端口不可用: %w", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	ctx, cancel := context.WithCancel(context.Background())
	receiver, err := lan.NewReceiver(lan.ReceiverConfig{
		InboxDir:   s.cfg.Inbox,
		Secret:     secret,
		Identity:   identity,
		InstanceID: s.cfg.InstanceID,
		OnProgress: func(filename string, done, total int64) {
			s.emit("progress", map[string]any{
				"kind": "recv", "filename": filename, "done": done, "total": total,
			})
		},
		OnReceived: s.recordReceived,
		OnStatus:   func(message string) { s.emit("status", message) },
	})
	if err != nil {
		cancel()
		_ = listener.Close()
		s.mu.Unlock()
		return err
	}
	discovery, err := lan.Start(lan.Config{
		PeerName:     s.cfg.PeerName,
		InstanceID:   s.cfg.InstanceID,
		Port:         port,
		Identity:     identity,
		Capabilities: []string{lan.CapReliable, lan.CapFolder, lan.CapWHE4},
		LocalIPs:     lan.LocalIPv4s(),
	}, s.updateLANPeers, func(message string) { s.emit("status", message) })
	if err != nil {
		cancel()
		_ = listener.Close()
		s.mu.Unlock()
		return err
	}
	token, err := randomToken()
	if err != nil {
		discovery.Stop()
		cancel()
		_ = listener.Close()
		s.mu.Unlock()
		return err
	}
	transport := transportcore.NewService()
	if _, err := callTransport(transport, "start", map[string]any{
		"local_target": net.JoinHostPort("127.0.0.1", strconv.Itoa(port)),
		"local_token":  token, "device_name": s.cfg.PeerName,
		"instance_id": s.cfg.InstanceID,
	}); err != nil {
		_ = transport.Close()
		discovery.Stop()
		cancel()
		_ = listener.Close()
		s.mu.Unlock()
		return err
	}
	s.identity = identity
	s.secret = secret
	s.ctx = ctx
	s.cancel = cancel
	s.listener = listener
	s.receiver = receiver
	s.discovery = discovery
	s.ingressToken = token
	s.actualPort = port
	s.transport = transport
	s.conns = make(map[net.Conn]struct{})
	s.running = true
	s.nodeWG.Add(3)
	go s.acceptLoop(listener, receiver, identity, token)
	go s.manualProbeLoop(ctx)
	go s.transportEventLoop(ctx, transport)
	sshEnabled := s.cfg.CrossNetwork.SSH.Enabled
	peerName := s.cfg.PeerName
	warning := s.credentialWarning
	s.mu.Unlock()
	s.emit("status", fmt.Sprintf("墨洞已开启 · %s", peerName))
	if warning != "" {
		s.emit("status", warning)
	}
	if sshEnabled {
		go func() {
			if err := s.startSSH(); err != nil {
				s.emit("transport", map[string]any{"event": "ssh.config.error", "error": err.Error()})
			}
		}()
	}
	return nil
}

func (s *InkHoleService) activeTransferSecretLocked() (string, error) {
	if !s.cfg.EncryptionEnabled {
		return "", nil
	}
	secret, err := s.credentials.Get(transferCredential)
	if err != nil {
		if s.legacySecret != "" {
			return s.legacySecret, nil
		}
		return "", fmt.Errorf("无法读取端到端口令；为避免明文传输，墨洞未启动: %w", err)
	}
	if secret == "" {
		secret = s.legacySecret
	}
	if secret == "" {
		return "", errors.New("已启用端到端加密，但未找到已保存的口令")
	}
	return secret, nil
}

func (s *InkHoleService) loadIdentityLocked() (*lan.Identity, error) {
	encoded, err := s.credentials.Get(identityCredential)
	if err != nil {
		encoded = s.legacyIdentity
		s.credentialWarning = "系统安全存储不可用，本次运行使用临时设备身份"
	}
	if encoded != "" {
		if identity, loadErr := lan.IdentityFromPrivateKey(encoded); loadErr == nil {
			return identity, nil
		}
	}
	identity, err := lan.GenerateIdentity()
	if err != nil {
		return nil, err
	}
	exported, err := identity.ExportPrivateKey()
	if err != nil {
		return nil, err
	}
	if err := s.credentials.Set(identityCredential, exported); err != nil {
		s.credentialWarning = "无法保存设备身份，本次运行使用临时身份"
	}
	return identity, nil
}

func randomToken() (string, error) {
	raw := make([]byte, 24)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func (s *InkHoleService) acceptLoop(listener net.Listener, receiver *lan.Receiver,
	identity *lan.Identity, ingressToken string) {
	defer s.nodeWG.Done()
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		if !s.trackConn(conn) {
			_ = conn.Close()
			return
		}
		s.nodeWG.Add(1)
		go func(conn net.Conn) {
			defer s.nodeWG.Done()
			defer s.untrackConn(conn)
			s.mu.Lock()
			name, instanceID := s.cfg.PeerName, s.cfg.InstanceID
			s.mu.Unlock()
			lan.HandleInbound(conn, lan.InboundConfig{
				IngressToken: ingressToken,
				Identity:     identity,
				InstanceID:   instanceID,
				PeerName:     func() string { return name },
				Capabilities: func() []string {
					return []string{lan.CapReliable, lan.CapFolder, lan.CapWHE4}
				},
				Receiver: receiver,
			})
		}(conn)
	}
}

// trackConn registers a live inbound connection; false once Stop began.
func (s *InkHoleService) trackConn(conn net.Conn) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conns == nil {
		return false
	}
	s.conns[conn] = struct{}{}
	return true
}

func (s *InkHoleService) untrackConn(conn net.Conn) {
	s.mu.Lock()
	if s.conns != nil {
		delete(s.conns, conn)
	}
	s.mu.Unlock()
}

func (s *InkHoleService) Stop() {
	s.mu.Lock()
	if !s.running {
		s.mu.Unlock()
		return
	}
	s.running = false
	cancel := s.cancel
	listener := s.listener
	discovery := s.discovery
	transport := s.transport
	s.listener = nil
	s.receiver = nil
	s.discovery = nil
	s.transport = nil
	s.actualPort = 0
	s.sshSessionID = ""
	s.sshConnected = false
	for _, cancelSend := range s.sends {
		cancelSend()
	}
	// Closing live inbound connections unblocks handlers mid-read so the
	// nodeWG.Wait below cannot hang on a stalled peer.
	for conn := range s.conns {
		_ = conn.Close()
	}
	s.conns = nil
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	// Close listener BEFORE stopping discovery, so goodbye probe confirms departure
	if listener != nil {
		_ = listener.Close()
	}
	if discovery != nil {
		discovery.Stop()
	}
	if transport != nil {
		_ = transport.Close()
	}
	s.nodeWG.Wait()
	s.sendWG.Wait()
	s.mu.Lock()
	s.lanPeers = make(map[string]lan.Peer)
	s.manualRuntime = make(map[string]PeerView)
	s.manualStrikes = make(map[string]int)
	s.sshRuntime = make(map[string]runtimeSSHPeer)
	s.endpointWHE4 = make(map[string]bool)
	s.sends = make(map[string]context.CancelFunc)
	s.mu.Unlock()
}

func (s *InkHoleService) Close() { s.Stop() }

func (s *InkHoleService) updateLANPeers(peers []lan.Peer) {
	s.mu.Lock()
	s.lanPeers = make(map[string]lan.Peer, len(peers))
	for _, peer := range peers {
		s.lanPeers[peer.InstanceID] = peer
	}
	views := s.peerViewsLocked()
	s.mu.Unlock()
	s.emit("peers", views)
}

func (s *InkHoleService) peerViewsLocked() []PeerView {
	merged := make(map[string]PeerView)
	add := func(peer PeerView, route string) {
		if peer.InstanceID == "" {
			return
		}
		current, exists := merged[peer.InstanceID]
		if !exists {
			peer.Routes = []string{route}
			peer.Transport = route
			merged[peer.InstanceID] = peer
			return
		}
		for _, value := range current.Routes {
			if value == route {
				return
			}
		}
		current.Routes = append(current.Routes, route)
		current.Transport = strings.Join(current.Routes, " + ")
		merged[peer.InstanceID] = current
	}
	for _, peer := range s.lanPeers {
		add(PeerView{InstanceID: peer.InstanceID, Name: peer.Name, Host: peer.Host,
			Port: peer.Port, Fingerprint: peer.Fingerprint}, "局域网")
	}
	for _, peer := range s.manualRuntime {
		add(peer, "Tailscale/固定地址")
	}
	for _, peer := range s.sshRuntime {
		host, port := splitEndpoint(peer.Endpoint)
		add(PeerView{InstanceID: peer.InstanceID, Name: peer.Name, Host: host,
			Port: port}, "SSH/QUIC")
	}
	result := make([]PeerView, 0, len(merged))
	for _, peer := range merged {
		result = append(result, peer)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Name == result[j].Name {
			return result[i].InstanceID < result[j].InstanceID
		}
		return result[i].Name < result[j].Name
	})
	return result
}

func (s *InkHoleService) Peers() []PeerView {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.peerViewsLocked()
}

// RefreshDiscovery drives discovery hard for a few seconds: an announcement
// burst, back-to-back mDNS sweeps and a faster liveness cadence. The frontend
// calls it when the window comes forward or the user opens the device list,
// which is exactly when a stale list is visible and worth the extra traffic.
func (s *InkHoleService) RefreshDiscovery() {
	s.mu.Lock()
	discovery := s.discovery
	s.mu.Unlock()
	if discovery != nil {
		discovery.Refresh()
	}
}

func (s *InkHoleService) SelectPeer(instanceID string) error {
	instanceID = strings.ToLower(strings.TrimSpace(instanceID))
	if instanceID != "" && !lan.ValidInstanceID(instanceID) {
		return errors.New("目标设备标识无效")
	}
	s.mu.Lock()
	s.selected = instanceID
	s.mu.Unlock()
	s.emit("selection", map[string]any{"instanceId": instanceID})
	return nil
}

func (s *InkHoleService) GetSelected() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.selected
}

func (s *InkHoleService) SendPaths(instanceID string, paths []string) (string, error) {
	s.mu.Lock()
	if !s.running || s.identity == nil || s.ctx == nil {
		s.mu.Unlock()
		return "", errors.New("墨洞未开启")
	}
	instanceID = strings.ToLower(strings.TrimSpace(instanceID))
	if instanceID == "" {
		instanceID = s.selected
		if instanceID == "" {
			peers := s.peerViewsLocked()
			if len(peers) == 1 {
				instanceID = peers[0].InstanceID
				s.selected = instanceID
			}
		}
	}
	routes := s.routesLocked(instanceID)
	if len(routes) == 0 {
		s.mu.Unlock()
		return "", errors.New("目标设备不在线")
	}
	identity, selfID, secret := s.identity, s.cfg.InstanceID, s.secret
	outgoingStatePath := filepath.Join(s.cfg.Inbox, ".inkhole-outgoing.json")
	ctx, cancel := context.WithCancel(s.ctx)
	sendID := fmt.Sprintf("send-%d", time.Now().UnixNano())
	s.sends[sendID] = cancel
	s.sendWG.Add(1)
	s.mu.Unlock()
	normalized, err := normalizeSendPaths(paths)
	if err != nil {
		s.finishSend(sendID)
		return "", err
	}
	go s.runSendBatch(ctx, sendID, normalized, routes, identity, selfID, secret,
		outgoingStatePath)
	return sendID, nil
}

func (s *InkHoleService) SendToSelected(paths []string) (string, error) {
	return s.SendPaths("", paths)
}

func (s *InkHoleService) CancelSend(sendID string) bool {
	s.mu.Lock()
	cancel := s.sends[sendID]
	s.mu.Unlock()
	if cancel == nil {
		return false
	}
	cancel()
	return true
}

func normalizeSendPaths(paths []string) ([]string, error) {
	if len(paths) == 0 {
		return nil, errors.New("没有选择要发送的内容")
	}
	if len(paths) > 128 {
		return nil, errors.New("单次最多发送 128 项")
	}
	result := make([]string, 0, len(paths))
	seen := make(map[string]bool)
	for _, value := range paths {
		path, err := filepath.Abs(strings.TrimSpace(value))
		if err != nil {
			return nil, err
		}
		if seen[path] {
			continue
		}
		if _, err := os.Stat(path); err != nil {
			return nil, fmt.Errorf("无法读取 %s: %w", filepath.Base(path), err)
		}
		seen[path] = true
		result = append(result, path)
	}
	if len(result) == 0 {
		return nil, errors.New("没有可发送的内容")
	}
	return result, nil
}

func (s *InkHoleService) runSendBatch(ctx context.Context, sendID string,
	paths []string, routes []lan.SendTarget, identity *lan.Identity,
	instanceID, secret, outgoingStatePath string) {
	defer s.finishSend(sendID)
	s.resolveEndpointRoutes(ctx, routes)
	s.emit("transfer-start", map[string]any{"sendId": sendID, "total": len(paths)})
	succeeded := 0
	for _, path := range paths {
		var sendErr error
		for routeIndex, target := range routes {
			if ctx.Err() != nil {
				sendErr = ctx.Err()
				break
			}
			cfg := lan.SenderConfig{
				Secret: secret, Identity: identity, InstanceID: instanceID,
				OutgoingStatePath: outgoingStatePath,
				OnProgress: func(filename string, done, total int64) {
					s.emit("progress", map[string]any{"kind": "send", "sendId": sendID,
						"filename": filename, "done": done, "total": total})
				},
				OnStatus: func(message string) { s.emit("status", message) },
			}
			info, statErr := os.Stat(path)
			if statErr != nil {
				sendErr = statErr
				break
			}
			if info.IsDir() {
				sendErr = lan.SendFolder(ctx, target, path, cfg)
			} else {
				sendErr = lan.SendFile(ctx, target, path, cfg)
			}
			if sendErr == nil {
				break
			}
			if routeIndex+1 < len(routes) {
				s.emit("status", "当前通道不可用，正在切换备用通道")
			}
		}
		payload := map[string]any{"sendId": sendID, "path": path,
			"name": filepath.Base(path), "ok": sendErr == nil}
		if sendErr != nil {
			payload["error"] = sendErr.Error()
		} else {
			succeeded++
		}
		s.emit("sent", payload)
	}
	s.emit("transfer-finished", map[string]any{
		"sendId": sendID, "succeeded": succeeded, "total": len(paths),
	})
}

// resolveEndpointRoutes probes SSH/wormhole bridge endpoints with WHPC once
// per batch so cross-network sends can negotiate WHE4 and pin the receiver
// identity. Peers that never answer (older cores) keep the WHE3 fallback.
func (s *InkHoleService) resolveEndpointRoutes(ctx context.Context, routes []lan.SendTarget) {
	for index := range routes {
		if ctx.Err() != nil {
			return
		}
		target := &routes[index]
		if target.EndpointToken == "" || target.UseWHE4 {
			continue
		}
		if target.InstanceID != "" {
			s.mu.Lock()
			cached, known := s.endpointWHE4[target.InstanceID]
			s.mu.Unlock()
			if known {
				target.UseWHE4 = cached
				continue
			}
		}
		probe, err := lan.ProbeEndpoint(target.Host, target.Port,
			target.EndpointToken, 6*time.Second, target.InstanceID)
		if err != nil {
			continue
		}
		target.UseWHE4 = lan.SupportsWHE4(probe.Capabilities)
		if target.InstanceID == "" {
			target.InstanceID = probe.InstanceID
		}
		if target.Fingerprint == "" {
			target.Fingerprint = probe.Fingerprint
		}
		s.mu.Lock()
		s.endpointWHE4[target.InstanceID] = target.UseWHE4
		s.mu.Unlock()
	}
}

func (s *InkHoleService) finishSend(sendID string) {
	s.mu.Lock()
	if cancel := s.sends[sendID]; cancel != nil {
		cancel()
		delete(s.sends, sendID)
		s.sendWG.Done()
	}
	s.mu.Unlock()
}

func (s *InkHoleService) routesLocked(instanceID string) []lan.SendTarget {
	var result []lan.SendTarget
	if peer, ok := s.lanPeers[instanceID]; ok {
		result = append(result, lan.SendTarget{Host: peer.Host, Hosts: peer.Hosts,
			Port: peer.Port, InstanceID: peer.InstanceID, Fingerprint: peer.Fingerprint,
			UseWHE4: lan.SupportsWHE4(peer.Capabilities)})
	}
	for _, peer := range s.manualRuntime {
		if peer.InstanceID == instanceID {
			result = append(result, lan.SendTarget{Host: peer.Host, Port: peer.Port,
				InstanceID: peer.InstanceID, Fingerprint: peer.Fingerprint,
				UseWHE4: lan.SupportsWHE4(peer.Capabilities)})
		}
	}
	if peer, ok := s.sshRuntime[instanceID]; ok {
		host, port := splitEndpoint(peer.Endpoint)
		if host != "" && port > 0 {
			result = append(result, lan.SendTarget{Host: host, Port: port,
				InstanceID: peer.InstanceID, EndpointToken: peer.EndpointToken})
		}
	}
	return result
}

func splitEndpoint(endpoint string) (string, int) {
	host, rawPort, err := net.SplitHostPort(endpoint)
	if err != nil {
		return "", 0
	}
	port, err := strconv.Atoi(rawPort)
	if err != nil {
		return "", 0
	}
	return host, port
}

func (s *InkHoleService) recordReceived(path string) {
	s.mu.Lock()
	filtered := []string{path}
	for _, existing := range s.cfg.RecentFiles {
		if existing != path && len(filtered) < maximumRecentFiles {
			filtered = append(filtered, existing)
		}
	}
	s.cfg.RecentFiles = filtered
	_ = s.saveConfigLocked()
	s.mu.Unlock()
	s.emit("received", map[string]any{"path": path, "name": filepath.Base(path)})
}

func (s *InkHoleService) RecentFiles() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	result := make([]string, 0, len(s.cfg.RecentFiles))
	for _, path := range s.cfg.RecentFiles {
		if _, err := os.Stat(path); err == nil {
			result = append(result, path)
		}
	}
	return result
}

func (s *InkHoleService) ClearRecent() error {
	s.mu.Lock()
	s.cfg.RecentFiles = nil
	err := s.saveConfigLocked()
	s.mu.Unlock()
	if err == nil {
		s.emit("recent", []string{})
	}
	return err
}

func (s *InkHoleService) OpenPath(path string) error {
	path = filepath.Clean(path)
	if _, err := os.Stat(path); err != nil {
		return err
	}
	if s.app == nil {
		return errors.New("应用尚未启动")
	}
	return s.app.Browser.OpenFile(path)
}

func (s *InkHoleService) OpenInbox() error {
	s.mu.Lock()
	inbox := s.cfg.Inbox
	s.mu.Unlock()
	if err := os.MkdirAll(inbox, 0o755); err != nil {
		return err
	}
	return s.OpenPath(inbox)
}

func (s *InkHoleService) ChooseFiles() ([]string, error) {
	if s.app == nil {
		return nil, errors.New("应用尚未启动")
	}
	return s.app.Dialog.OpenFile().SetTitle("选择要发送的文件").
		CanChooseFiles(true).CanChooseDirectories(false).PromptForMultipleSelection()
}

func (s *InkHoleService) ChooseFolder() (string, error) {
	if s.app == nil {
		return "", errors.New("应用尚未启动")
	}
	return s.app.Dialog.OpenFile().SetTitle("选择要发送的文件夹").
		CanChooseFiles(false).CanChooseDirectories(true).PromptForSingleSelection()
}

func (s *InkHoleService) ChooseInbox() (string, error) {
	if s.app == nil {
		return "", errors.New("应用尚未启动")
	}
	s.mu.Lock()
	current := s.cfg.Inbox
	s.mu.Unlock()
	return s.app.Dialog.OpenFile().SetTitle("选择默认目录").SetDirectory(current).
		CanChooseFiles(false).CanChooseDirectories(true).PromptForSingleSelection()
}

func (s *InkHoleService) ShowMain() {
	if s.app == nil {
		return
	}
	if window, ok := s.app.Window.GetByName("main"); ok && window != nil {
		window.Show()
		window.Focus()
	}
}

func (s *InkHoleService) SetPetVisible(visible bool) {
	if s.app == nil {
		return
	}
	if window, ok := s.app.Window.GetByName("pet"); ok && window != nil {
		if visible {
			window.Show()
		} else {
			window.Hide()
		}
	}
}

func (s *InkHoleService) manualProbeLoop(ctx context.Context) {
	defer s.nodeWG.Done()
	for {
		s.probeManualPeers()
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

// manualProbeStrikes mirrors PROBE_STRIKES_MANUAL on Android: Tailscale
// wakes lazily after idle (hole punch / DERP setup), so the first probe
// regularly times out; a verified device only leaves the list after this
// many consecutive failed rounds.
const manualProbeStrikes = 4

func (s *InkHoleService) probeManualPeers() {
	s.mu.Lock()
	peers := append([]ManualPeerConfig(nil), s.cfg.ManualPeers...)
	running := s.running
	if running {
		s.manualProbeGen++
	}
	generation := s.manualProbeGen
	probePeer := s.manualProbe
	s.mu.Unlock()
	if !running {
		return
	}
	if probePeer == nil {
		probePeer = lan.ProbePeer
	}
	type result struct {
		key        string
		configured ManualPeerConfig
		peer       PeerView
	}
	results := make(chan result, len(peers))
	var wg sync.WaitGroup
	for _, configured := range peers {
		if configured.Host == "" || configured.Port < 1 || configured.Port > 65535 {
			continue
		}
		wg.Add(1)
		go func(configured ManualPeerConfig) {
			defer wg.Done()
			probe, err := probePeer(configured.Host, configured.Port,
				3*time.Second, configured.InstanceID)
			if err != nil {
				return
			}
			name := configured.Name
			if strings.TrimSpace(name) == "" {
				name = probe.PeerName
			}
			results <- result{key: manualPeerKey(configured), configured: configured,
				peer: PeerView{InstanceID: probe.InstanceID,
					Name: name, Host: configured.Host, Port: configured.Port,
					Fingerprint: probe.Fingerprint, Transport: "Tailscale/固定地址",
					Capabilities: probe.Capabilities}}
		}(configured)
	}
	wg.Wait()
	close(results)
	probeResults := make(map[string]result, len(peers))
	for value := range results {
		probeResults[value.key] = value
	}
	runtimePeers := make(map[string]PeerView)
	changed := false
	s.mu.Lock()
	if generation != s.manualProbeGen {
		s.mu.Unlock()
		return
	}
	strikes := make(map[string]int, len(s.cfg.ManualPeers))
	for index := range s.cfg.ManualPeers {
		configured := &s.cfg.ManualPeers[index]
		key := manualPeerKey(*configured)
		if value, ok := probeResults[key]; ok &&
			configured.InstanceID == value.configured.InstanceID &&
			configured.Fingerprint == value.configured.Fingerprint {
			runtimePeers[value.peer.InstanceID] = value.peer
			if configured.InstanceID != value.peer.InstanceID ||
				configured.Fingerprint != value.peer.Fingerprint {
				configured.InstanceID = value.peer.InstanceID
				configured.Fingerprint = value.peer.Fingerprint
				changed = true
			}
			continue
		}
		strikes[key] = s.manualStrikes[key] + 1
		if strikes[key] >= manualProbeStrikes || configured.InstanceID == "" {
			continue
		}
		if previous, ok := s.manualRuntime[configured.InstanceID]; ok {
			runtimePeers[configured.InstanceID] = previous
		}
	}
	s.manualStrikes = strikes
	s.manualRuntime = runtimePeers
	if changed {
		_ = s.saveConfigLocked()
	}
	views := s.peerViewsLocked()
	s.mu.Unlock()
	s.emit("peers", views)
}

func manualPeerKey(peer ManualPeerConfig) string {
	return strings.ToLower(strings.TrimSpace(peer.Host)) + ":" + strconv.Itoa(peer.Port)
}

func (s *InkHoleService) ManualPeers() []ManualPeerConfig {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]ManualPeerConfig(nil), s.cfg.ManualPeers...)
}

func (s *InkHoleService) SaveManualPeers(peers []ManualPeerConfig) error {
	if len(peers) > maximumManualPeers {
		return fmt.Errorf("手动设备最多 %d 个", maximumManualPeers)
	}
	normalized := make([]ManualPeerConfig, 0, len(peers))
	seen := make(map[string]bool)
	for _, peer := range peers {
		peer.Name = strings.TrimSpace(peer.Name)
		peer.Host = strings.TrimSpace(peer.Host)
		if peer.Host == "" || peer.Port < 1 || peer.Port > 65535 ||
			strings.ContainsAny(peer.Host, "\r\n\x00") {
			return errors.New("手动设备地址或端口无效")
		}
		key := strings.ToLower(peer.Host) + ":" + strconv.Itoa(peer.Port)
		if seen[key] {
			continue
		}
		seen[key] = true
		if !lan.ValidInstanceID(strings.ToLower(peer.InstanceID)) {
			peer.InstanceID = ""
		} else {
			peer.InstanceID = strings.ToLower(peer.InstanceID)
		}
		normalized = append(normalized, peer)
	}
	s.mu.Lock()
	s.manualProbeGen++
	s.cfg.ManualPeers = normalized
	err := s.saveConfigLocked()
	s.mu.Unlock()
	if err == nil {
		go s.probeManualPeers()
	}
	return err
}
