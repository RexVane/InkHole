import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../models.dart';
import '../theme.dart';
import '../widgets/cross_network_dialog.dart' show FingerprintConfirmDialog;

/// SSH 反向隧道中继控制台。
///
/// 本页是**纯展示**层：SSH 中继的全部状态由 home_page 持有（见
/// [SshRelayStatus]），通过 [relay] 这个 ValueListenable 推过来，动作则通过
/// 回调交回 home_page 去调用核心。这样做的原因有两个：
///   1. 核心事件流是单一广播源，监听与去重集中在 home_page 一处；
///   2. 本页是 Navigator.push 出来的路由，父页面 setState 不会重建它，
///      所以必须用可监听对象而不是普通参数来传递实时状态。
///
/// 与设计稿的**有意偏差**（都是为了不说谎，逐条说明）：
///   - 设计稿有「Ed25519 密钥认证 / 密码认证」页签：核心的 `SshProfile` 只有
///     `private_key` + `passphrase`（密钥口令），不支持密码登录，故只保留密钥认证。
///   - 设计稿的「墨洞本机 Ed25519 公钥 → 复制 → 加进 VPS authorized_keys」：
///     核心用**你提供的私钥**登录，`ssh.listen` 回传的 `noise_public` 是设备
///     身份公钥（给对端配对握手用的），拿它去 VPS 登录是登不上的。故这里改成
///     导入私钥 + 密钥口令 + 主机指纹——核心真正需要的三项。
///   - 设计稿的「动态流压缩 (zlib)」：核心没有实现，删除。
///   - 设计稿的「连接保活 / 自动重连」开关：核心里是硬编码常开
///     （`SSH_KEEPALIVE_INTERVAL = 30s`、重连退避固定），无法关闭，
///     故改为只读状态展示而不是可以拨动却无效的开关。
///   - 设计稿的「~14ms RTT」：核心没有 RTT 接口，改为显示**实测**的
///     `ssh.check` 握手耗时。
class SshRelayView extends StatefulWidget {
  const SshRelayView({
    super.key,
    required this.initial,
    required this.localPort,
    required this.relay,
    required this.onBack,
    required this.onCheckHandshake,
    required this.onSaveFingerprint,
    required this.onConnect,
    required this.onDisconnect,
    required this.onCreatePairCode,
    required this.onJoinPairCode,
    required this.onClearLogs,
  });

  final InkSettings initial;

  /// 本机 LAN QUIC 监听端口，用于「L:x → R:y」这一行。
  final int localPort;

  final ValueListenable<SshRelayStatus> relay;

  final VoidCallback onBack;

  /// 跑一次 `ssh.check`；失败返回 null（错误由 home_page 提示）。
  final Future<SshHandshakeResult?> Function(InkSettings draft) onCheckHandshake;

  /// 用户在指纹确认弹窗里点了「确认并固定」。
  final Future<void> Function(String fingerprint) onSaveFingerprint;

  /// 保存并建立隧道。
  final Future<void> Function(InkSettings draft) onConnect;

  final Future<void> Function() onDisconnect;
  final Future<void> Function() onCreatePairCode;
  final Future<void> Function(String code) onJoinPairCode;
  final VoidCallback onClearLogs;

  @override
  State<SshRelayView> createState() => _SshRelayViewState();
}

class _SshRelayViewState extends State<SshRelayView> {
  late final TextEditingController _host;
  late final TextEditingController _port;
  late final TextEditingController _user;
  late final TextEditingController _fingerprint;
  late final TextEditingController _privateKey;
  late final TextEditingController _passphrase;
  late final TextEditingController _remotePort;
  final TextEditingController _joinCode = TextEditingController();

  bool _keyVisible = false;
  bool _busy = false;
  SshHandshakeResult? _handshake;

  @override
  void initState() {
    super.initState();
    final InkSettings initial = widget.initial;
    _host = TextEditingController(text: initial.sshHost);
    _port = TextEditingController(
      text: initial.sshPort > 0 ? initial.sshPort.toString() : '22',
    );
    _user = TextEditingController(text: initial.sshUser);
    _fingerprint = TextEditingController(text: initial.sshFingerprint);
    _privateKey = TextEditingController(text: initial.sshPrivateKey);
    _passphrase = TextEditingController(text: initial.sshPassphrase);
    _remotePort = TextEditingController(
      text: initial.sshRemotePort.toString(),
    );
  }

  @override
  void dispose() {
    _host.dispose();
    _port.dispose();
    _user.dispose();
    _fingerprint.dispose();
    _privateKey.dispose();
    _passphrase.dispose();
    _remotePort.dispose();
    _joinCode.dispose();
    super.dispose();
  }

  int get _portValue => int.tryParse(_port.text.trim()) ?? 0;

  int get _remotePortValue => int.tryParse(_remotePort.text.trim()) ?? 0;

  InkSettings _draft() => InkSettings(
        peerName: widget.initial.peerName,
        listenPort: widget.initial.listenPort,
        encryptionEnabled: widget.initial.encryptionEnabled,
        secret: widget.initial.secret,
        manualPeers: widget.initial.manualPeers,
        rendezvousUrl: widget.initial.rendezvousUrl,
        transitRelay: widget.initial.transitRelay,
        // 透传而不是硬写 true：控制台只能从「SSH 中继」开关打开时进入，
        // 但保存动作不该顺手改动用户没碰过的开关。
        sshEnabled: widget.initial.sshEnabled,
        sshHost: _host.text.trim(),
        sshPort: _portValue,
        sshUser: _user.text.trim(),
        sshFingerprint: _fingerprint.text.trim(),
        sshPrivateKey: _privateKey.text.trim(),
        sshPassphrase: _passphrase.text,
        sshRemotePort: _remotePortValue,
      );

  /// 建立中继前的完整校验（含必须已固定指纹）。
  String? _validate() => validateSshRelayConfig(
        host: _host.text,
        port: _portValue,
        user: _user.text,
        fingerprint: _fingerprint.text,
        privateKey: _privateKey.text,
        remotePort: _remotePortValue,
      );

  /// 测试握手的校验：**不要求指纹**——指纹正是这次握手要取回来的东西。
  String? _validateForHandshake() => validateSshRelayConfig(
        host: _host.text,
        port: _portValue,
        user: _user.text,
        fingerprint: _fingerprint.text,
        privateKey: _privateKey.text,
        remotePort: _remotePortValue,
        requireFingerprint: false,
      );

  void _notify(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _importPrivateKey() async {
    try {
      final FilePickerResult? result = await FilePicker.platform.pickFiles(
        type: FileType.any,
        withData: true,
      );
      if (result == null || result.files.isEmpty) return;
      final PlatformFile file = result.files.first;
      String? text;
      if (file.bytes != null) {
        text = utf8.decode(file.bytes!, allowMalformed: true);
      } else {
        final String? path = file.path;
        if (path != null && path.isNotEmpty) {
          text = await File(path).readAsString();
        }
      }
      if (!mounted) return;
      if (text == null || text.trim().isEmpty) {
        _notify('读不到密钥内容，请改用粘贴');
        return;
      }
      setState(() => _privateKey.text = text!);
      _notify('已读入密钥：${file.name}');
    } catch (error) {
      if (mounted) _notify('导入密钥失败: $error');
    }
  }

  Future<void> _testHandshake() async {
    final String? invalid = _validateForHandshake();
    if (invalid != null) {
      _notify(invalid);
      return;
    }
    setState(() => _busy = true);
    try {
      final SshHandshakeResult? result =
          await widget.onCheckHandshake(_draft());
      if (!mounted) return;
      if (result == null) return;
      setState(() => _handshake = result);
      final String fixed = _fingerprint.text.trim();
      if (fixed == result.fingerprint) {
        _notify('握手成功，主机指纹与已固定的一致');
        return;
      }
      // 未固定过、或与本地记录不一致：交给用户核对。
      final String? confirmed = await showDialog<String>(
        context: context,
        builder: (BuildContext dialogContext) => FingerprintConfirmDialog(
          fingerprint: result.fingerprint,
          serverVersion: result.serverLabel,
        ),
      );
      if (!mounted || confirmed == null) return;
      setState(() => _fingerprint.text = confirmed);
      await widget.onSaveFingerprint(confirmed);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _connect() async {
    final String? invalid = _validate();
    if (invalid != null) {
      _notify(invalid);
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.onConnect(_draft());
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _disconnect() async {
    setState(() => _busy = true);
    try {
      await widget.onDisconnect();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _copy(String value, String label) async {
    await Clipboard.setData(ClipboardData(text: value));
    _notify('$label已复制');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: textMuted),
          onPressed: widget.onBack,
        ),
        title: const Text(
          'SSH 隧道中继',
          style: TextStyle(
            color: textPrimary,
            fontSize: 16,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
      body: ValueListenableBuilder<SshRelayStatus>(
        valueListenable: widget.relay,
        builder: (BuildContext context, SshRelayStatus status, Widget? _) {
          return SingleChildScrollView(
            physics: const BouncingScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 28),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                _buildHeader(status),
                const SizedBox(height: 12),
                _buildNarrative(),
                const SizedBox(height: 12),
                _buildTopology(status),
                const SizedBox(height: 12),
                _buildServerCard(status),
                const SizedBox(height: 12),
                _buildTunnelCard(),
                const SizedBox(height: 12),
                _buildPairingCard(status),
                const SizedBox(height: 12),
                _buildActions(status),
                const SizedBox(height: 12),
                _buildLogConsole(status),
                const SizedBox(height: 12),
                _buildSecurityNote(),
              ],
            ),
          );
        },
      ),
    );
  }

  // ---------- 顶部状态 ----------

  Widget _buildHeader(SshRelayStatus status) {
    final Color tone = status.connected
        ? jade400
        : (status.initialError.isNotEmpty ? dangerCoral : textMuted);
    final String label = status.starting
        ? '连接中 · CONNECTING'
        : status.connected
            ? '已连接 · CONNECTED'
            : status.active
                ? '未连通 · OFFLINE'
                : '就绪 · READY';

    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Container(
          width: 32,
          height: 32,
          decoration: BoxDecoration(
            color: surfaceContainer,
            shape: BoxShape.circle,
            border: Border.all(color: surfaceBorder),
          ),
          child: const Icon(Icons.terminal, size: 18, color: jade400),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const Text(
                'SSH 隧道中继 / Relay',
                style: TextStyle(
                  color: textPrimary,
                  fontSize: 15,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                'L:${widget.localPort} → R:${status.displayRemotePort} (QUIC/TLS)',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: textDim,
                  fontSize: 11,
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
        ),
        const SizedBox(width: 8),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            color: surfaceContainer,
            borderRadius: BorderRadius.circular(999),
            border: Border.all(color: tone.withValues(alpha: 0.35)),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              _LiveDot(color: tone),
              const SizedBox(width: 6),
              Text(
                label,
                style: TextStyle(
                  color: tone,
                  fontSize: 10,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 0.4,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }

  // ---------- 说明横幅 ----------

  Widget _buildNarrative() {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: surfaceHigh,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: surfaceBorder),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const Padding(
            padding: EdgeInsets.only(top: 1),
            child: Icon(Icons.shield_outlined, size: 18, color: jade400),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const Text(
                  '对称 NAT 零知识中继穿透',
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  '当局域网直连或 P2P 打洞失败时，墨洞通过你自有的主机建立加密反向中继通道。'
                  '中继端只转发端到端加密的 QUIC 密文，服务器对传输内容零知识。',
                  style: TextStyle(
                    color: textMuted.withValues(alpha: 0.95),
                    fontSize: 11,
                    height: 1.5,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  // ---------- 节点拓扑 ----------

  Widget _buildTopology(SshRelayStatus status) {
    final bool live = status.connected;
    final String pipeLabel =
        status.active ? 'SSH 反向隧道 · QUIC 密文' : '待建立';
    final String latency = _handshake == null
        ? '实测握手 —'
        : '实测握手 ${_handshake!.elapsedLabel}';

    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: borderLuminescent),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: <Widget>[
          _buildNode(
            icon: Icons.smartphone,
            title: '本端节点',
            detail: '127.0.0.1:${widget.localPort}',
            tone: jade400,
            animated: true,
          ),
          Expanded(
            child: Column(
              children: <Widget>[
                Row(
                  children: <Widget>[
                    Expanded(
                      child: _ParticleTrack(active: live, tone: jade400),
                    ),
                    Container(
                      width: 26,
                      height: 26,
                      margin: const EdgeInsets.symmetric(horizontal: 4),
                      decoration: BoxDecoration(
                        color: surfaceHigh,
                        shape: BoxShape.circle,
                        border: Border.all(
                          color: live ? jade400 : surfaceBorder,
                        ),
                      ),
                      child: Icon(
                        status.active ? Icons.lock : Icons.lock_open,
                        size: 13,
                        color: live ? jade400 : textDim,
                      ),
                    ),
                    Expanded(
                      child: _ParticleTrack(
                        active: live,
                        tone: secondaryTeal,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  pipeLabel,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: live ? jade300 : textDim,
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    fontFamily: 'monospace',
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  latency,
                  style: const TextStyle(
                    color: textDim,
                    fontSize: 9,
                    fontFamily: 'monospace',
                  ),
                ),
              ],
            ),
          ),
          _buildNode(
            icon: Icons.dns_outlined,
            title: '远端服务器',
            detail: status.displayRemotePort == 0
                ? '端口待分配'
                : ':${status.displayRemotePort} Remote',
            tone: secondaryTeal,
            animated: live,
          ),
        ],
      ),
    );
  }

  Widget _buildNode({
    required IconData icon,
    required String title,
    required String detail,
    required Color tone,
    required bool animated,
  }) {
    return SizedBox(
      width: 92,
      child: Column(
        children: <Widget>[
          SizedBox(
            height: 38,
            child: Stack(
              alignment: Alignment.center,
              children: <Widget>[
                if (animated) _PulseRing(color: tone),
                Container(
                  width: 34,
                  height: 34,
                  decoration: BoxDecoration(
                    color: surfaceActive,
                    shape: BoxShape.circle,
                    border: Border.all(color: tone.withValues(alpha: 0.35)),
                  ),
                  child: Icon(icon, size: 18, color: tone),
                ),
              ],
            ),
          ),
          const SizedBox(height: 4),
          Text(
            title,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: textPrimary,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            detail,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: textDim,
              fontSize: 9,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }

  // ---------- 服务器配置 ----------

  Widget _buildServerCard(SshRelayStatus status) {
    return _card(
      icon: Icons.router_outlined,
      title: '中继服务器节点',
      trailing: const Text(
        'NODE CONFIG',
        style: TextStyle(
          color: textDim,
          fontSize: 10,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.6,
        ),
      ),
      children: <Widget>[
        Row(
          children: <Widget>[
            Expanded(
              flex: 2,
              child: _field(
                label: '远程服务器地址 / IP',
                controller: _host,
                hint: 'e.g. 192.168.1.100',
                mono: true,
              ),
            ),
            const SizedBox(width: 8),
            SizedBox(
              width: 92,
              child: _field(
                label: 'SSH 端口',
                controller: _port,
                hint: '22',
                keyboardType: TextInputType.number,
                mono: true,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        _field(
          label: '登录用户 (SSH User)',
          controller: _user,
          hint: 'root',
          mono: true,
          leadingIcon: Icons.person_outline,
        ),
        const SizedBox(height: 8),
        _field(
          label: '主机指纹 (SHA256) — 必填',
          controller: _fingerprint,
          hint: 'SHA256:...',
          mono: true,
          leadingIcon: Icons.verified_user_outlined,
          trailing: _fingerprint.text.trim().isEmpty
              ? null
              : IconButton(
                  tooltip: '复制指纹',
                  icon: const Icon(Icons.content_copy, size: 16),
                  color: textDim,
                  onPressed: () => _copy(_fingerprint.text.trim(), '主机指纹'),
                ),
        ),
        const SizedBox(height: 8),
        // 核心真正需要的凭据：OpenSSH 私钥 + 可选的密钥口令。
        // 设计稿这里放的是"本机公钥拿去加 authorized_keys"，与核心认证方式不符，
        // 照做会让用户把一把用不上的钥匙加到服务器上。
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: surfaceHigh,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Row(
                children: <Widget>[
                  const Icon(Icons.key_outlined, size: 16, color: jade400),
                  const SizedBox(width: 6),
                  const Expanded(
                    child: Text(
                      'SSH 私钥 (OpenSSH)',
                      style: TextStyle(
                        color: textPrimary,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  TextButton.icon(
                    onPressed: _busy ? null : _importPrivateKey,
                    icon: const Icon(Icons.file_open_outlined, size: 15),
                    label: const Text('从文件导入', style: TextStyle(fontSize: 11)),
                    style: TextButton.styleFrom(
                      foregroundColor: jade400,
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      minimumSize: const Size(0, 32),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              TextField(
                key: const Key('ssh-private-key'),
                controller: _privateKey,
                maxLines: _keyVisible ? 6 : 2,
                minLines: 2,
                style: const TextStyle(
                  color: jade300,
                  fontSize: 11,
                  fontFamily: 'monospace',
                  height: 1.4,
                ),
                decoration: InputDecoration(
                  isDense: true,
                  filled: true,
                  fillColor: surfaceLowest,
                  hintText: '-----BEGIN OPENSSH PRIVATE KEY-----',
                  hintStyle: const TextStyle(color: textDim, fontSize: 11),
                  contentPadding: const EdgeInsets.all(10),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(8),
                    borderSide: BorderSide.none,
                  ),
                  suffixIcon: IconButton(
                    tooltip: _keyVisible ? '隐藏' : '展开',
                    icon: Icon(
                      _keyVisible
                          ? Icons.visibility_off_outlined
                          : Icons.visibility_outlined,
                      size: 16,
                    ),
                    color: textDim,
                    onPressed: () => setState(() => _keyVisible = !_keyVisible),
                  ),
                ),
              ),
              const SizedBox(height: 8),
              _field(
                label: '密钥口令 (passphrase，无则留空)',
                controller: _passphrase,
                hint: '密钥本身未加密时留空',
                obscure: true,
                mono: true,
              ),
              const SizedBox(height: 6),
              const Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Padding(
                    padding: EdgeInsets.only(top: 1),
                    child: Icon(Icons.info_outline, size: 13, color: textMuted),
                  ),
                  SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      '私钥仅保存在系统安全存储 (Android Keystore / iOS Keychain)，'
                      '不会上传到任何服务器，也不会随配对码发送给对端。',
                      style: TextStyle(
                        color: textMuted,
                        fontSize: 10.5,
                        height: 1.4,
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
        if (status.initialError.isNotEmpty) ...<Widget>[
          const SizedBox(height: 8),
          _inlineNotice(
            icon: Icons.error_outline,
            tone: dangerCoral,
            text: '上次建立会话时出错：${status.initialError}',
          ),
        ],
      ],
    );
  }

  // ---------- 转发与流控 ----------

  Widget _buildTunnelCard() {
    return _card(
      icon: Icons.alt_route,
      title: '转发与隧道流控',
      trailing: Text(
        _remotePortValue == sshRemotePortAuto
            ? 'R:自动'
            : 'R:$_remotePortValue',
        style: const TextStyle(
          color: badgeRoute,
          fontSize: 10,
          fontWeight: FontWeight.w600,
          fontFamily: 'monospace',
        ),
      ),
      children: <Widget>[
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: surfaceHigh,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Row(
            children: <Widget>[
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(
                      '远程监听端口 (Remote Port)',
                      style: TextStyle(
                        color: textPrimary,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    SizedBox(height: 2),
                    Text(
                      '0 = 交给服务器自动分配',
                      style: TextStyle(color: textDim, fontSize: 10),
                    ),
                  ],
                ),
              ),
              SizedBox(
                width: 76,
                child: TextField(
                  key: const Key('ssh-remote-port'),
                  controller: _remotePort,
                  textAlign: TextAlign.right,
                  keyboardType: TextInputType.number,
                  onChanged: (_) => setState(() {}),
                  style: const TextStyle(
                    color: jade400,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                    fontFamily: 'monospace',
                  ),
                  decoration: const InputDecoration(
                    isDense: true,
                    filled: false,
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    contentPadding: EdgeInsets.zero,
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 10),
        // 核心把这两项做成了硬编码常开（SSH_KEEPALIVE_INTERVAL 30s、重连退避
        // 固定），FFI 没有开关；所以这里只陈述事实，不提供拨不动的假开关。
        _alwaysOnRow(
          title: '连接保活心跳 (KeepAlive)',
          badge: '30s',
          detail: '每 30 秒发送一次保活探测，抑制蜂窝网络 NAT 超时降级。由核心常开。',
        ),
        const Divider(height: 18, color: surfaceBorder),
        _alwaysOnRow(
          title: '网络切换自动重连',
          badge: '常开',
          detail: 'Wi-Fi / 蜂窝切换时由核心自动以指数退避重连，不能关闭，也没有可调间隔。',
        ),
        const Divider(height: 18, color: surfaceBorder),
        _removedRow(),
      ],
    );
  }

  Widget _alwaysOnRow({
    required String title,
    required String badge,
    required String detail,
  }) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Flexible(
                    child: Text(
                      title,
                      style: const TextStyle(
                        color: textPrimary,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  const SizedBox(width: 6),
                  Text(
                    badge,
                    style: const TextStyle(
                      color: jade400,
                      fontSize: 10,
                      fontFamily: 'monospace',
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 2),
              Text(
                detail,
                style: const TextStyle(
                  color: textDim,
                  fontSize: 10,
                  height: 1.35,
                ),
              ),
            ],
          ),
        ),
        const SizedBox(width: 8),
        Container(
          margin: const EdgeInsets.only(top: 2),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration: BoxDecoration(
            color: surfaceHigh,
            borderRadius: BorderRadius.circular(999),
            border: Border.all(color: borderLuminescent),
          ),
          child: const Text(
            '常开',
            style: TextStyle(
              color: textMuted,
              fontSize: 9.5,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
  }

  Widget _removedRow() {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        const Padding(
          padding: EdgeInsets.only(top: 1),
          child: Icon(Icons.info_outline, size: 14, color: textDim),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: RichText(
            text: TextSpan(
              style: const TextStyle(
                color: textDim,
                fontSize: 10,
                height: 1.4,
              ),
              children: <InlineSpan>[
                const TextSpan(text: '设计稿中的「动态流压缩 (zlib stream)」被移除：'),
                TextSpan(
                  text: '核心并未实现流压缩',
                  style: TextStyle(color: textMuted.withValues(alpha: 0.9)),
                ),
                const TextSpan(text: '，保留一个不生效的开关只会误导人。'),
              ],
            ),
          ),
        ),
      ],
    );
  }

  // ---------- 配对 ----------

  Widget _buildPairingCard(SshRelayStatus status) {
    return _card(
      icon: Icons.hub_outlined,
      title: '设备配对',
      trailing: Text(
        '${status.peers.length} 台已配对',
        style: const TextStyle(
          color: textDim,
          fontSize: 10,
          fontWeight: FontWeight.w600,
        ),
      ),
      children: <Widget>[
        const Text(
          '设备之间用一次性配对码完成 PAKE 握手。配对码只在本次中继会话内有效，'
          '成功配对即作废，失败尝试超过上限也会作废。',
          style: TextStyle(color: textMuted, fontSize: 10.5, height: 1.45),
        ),
        const SizedBox(height: 10),
        if (status.pendingPairCode.isNotEmpty) ...<Widget>[
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: borderLuminescent),
            ),
            child: Row(
              children: <Widget>[
                Expanded(
                  child: SelectableText(
                    status.pendingPairCode,
                    style: const TextStyle(
                      color: jade300,
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                      fontFamily: 'monospace',
                      height: 1.4,
                    ),
                  ),
                ),
                IconButton(
                  tooltip: '复制配对码',
                  icon: const Icon(Icons.content_copy, size: 16),
                  color: jade400,
                  onPressed: () => _copy(status.pendingPairCode, '配对码'),
                ),
              ],
            ),
          ),
          const SizedBox(height: 8),
        ],
        _dashedAction(
          icon: Icons.qr_code_2,
          label: status.pendingPairCode.isEmpty ? '生成配对码（本机作为等待方）' : '重新生成配对码',
          enabled: !_busy && status.active,
          onTap: () async {
            setState(() => _busy = true);
            try {
              await widget.onCreatePairCode();
            } finally {
              if (mounted) setState(() => _busy = false);
            }
          },
        ),
        const SizedBox(height: 10),
        Row(
          children: <Widget>[
            Expanded(
              child: TextField(
                key: const Key('ssh-pair-code'),
                controller: _joinCode,
                style: const TextStyle(
                  color: textPrimary,
                  fontSize: 12,
                  fontFamily: 'monospace',
                ),
                decoration: InputDecoration(
                  isDense: true,
                  filled: true,
                  fillColor: surfaceHigh,
                  hintText: '输入对端显示的一次性配对码',
                  hintStyle: const TextStyle(color: textDim, fontSize: 11),
                  contentPadding:
                      const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(10),
                    borderSide: BorderSide.none,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: (_busy || !status.active)
                  ? null
                  : () async {
                      final String code = _joinCode.text.trim();
                      if (code.isEmpty) {
                        _notify('请输入对端显示的一次性配对码');
                        return;
                      }
                      setState(() => _busy = true);
                      try {
                        await widget.onJoinPairCode(code);
                        if (mounted) _joinCode.clear();
                      } finally {
                        if (mounted) setState(() => _busy = false);
                      }
                    },
              style: FilledButton.styleFrom(
                backgroundColor: surfaceActive,
                foregroundColor: jade400,
                minimumSize: const Size(0, 44),
                padding: const EdgeInsets.symmetric(horizontal: 16),
              ),
              child: const Text('加入', style: TextStyle(fontSize: 12)),
            ),
          ],
        ),
        if (!status.active) ...<Widget>[
          const SizedBox(height: 8),
          const Text(
            '先建立隧道，才能出码或输码配对。',
            style: TextStyle(color: textDim, fontSize: 10.5),
          ),
        ],
        const SizedBox(height: 12),
        // 对端身份列表：核心不会弹二次确认，所以把身份摊开给用户核对。
        const Text(
          '已配对设备（本次会话）',
          style: TextStyle(
            color: textMuted,
            fontSize: 10,
            fontWeight: FontWeight.w600,
            letterSpacing: 0.4,
          ),
        ),
        const SizedBox(height: 6),
        if (status.peers.isEmpty)
          Container(
            padding: const EdgeInsets.symmetric(vertical: 14),
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: surfaceBorder),
            ),
            child: const Text(
              '暂无对端。配对成功后，这里会显示对端名称与身份指纹，请核对无误。',
              textAlign: TextAlign.center,
              style: TextStyle(color: textDim, fontSize: 10.5, height: 1.4),
            ),
          )
        else
          for (final SshPeerInfo peer in status.peers)
            Container(
              margin: const EdgeInsets.only(bottom: 6),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: surfaceHigh,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Row(
                    children: <Widget>[
                      const Icon(Icons.devices_other,
                          size: 15, color: badgeRoute),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          peer.displayName,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: textPrimary,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      Text(
                        'R:${peer.remotePort}',
                        style: const TextStyle(
                          color: textDim,
                          fontSize: 10,
                          fontFamily: 'monospace',
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'ID  ${peer.shortId}',
                    style: const TextStyle(
                      color: textMuted,
                      fontSize: 10,
                      fontFamily: 'monospace',
                    ),
                  ),
                  Text(
                    'KEY ${peer.shortKey}',
                    style: const TextStyle(
                      color: textDim,
                      fontSize: 10,
                      fontFamily: 'monospace',
                    ),
                  ),
                ],
              ),
            ),
      ],
    );
  }

  // ---------- 主操作 ----------

  Widget _buildActions(SshRelayStatus status) {
    final bool active = status.active;
    return Row(
      children: <Widget>[
        Expanded(
          flex: 1,
          child: SizedBox(
            height: 46,
            child: OutlinedButton.icon(
              onPressed: _busy ? null : _testHandshake,
              icon: _busy
                  ? const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.network_check, size: 16),
              label: Text(
                _handshake == null ? '测试握手' : _handshake!.elapsedLabel,
                style: const TextStyle(fontSize: 12),
              ),
              style: OutlinedButton.styleFrom(
                foregroundColor: textPrimary,
                backgroundColor: surfaceContainer,
                side: const BorderSide(color: borderLuminescent),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(999),
                ),
              ),
            ),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          flex: 2,
          child: SizedBox(
            height: 46,
            child: FilledButton.icon(
              onPressed: _busy ? null : (active ? _disconnect : _connect),
              icon: Icon(
                active ? Icons.link_off : Icons.bolt,
                size: 18,
              ),
              label: Text(
                active ? '断开隧道' : '保存并建立隧道',
                style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
              ),
              style: FilledButton.styleFrom(
                backgroundColor: active ? dangerContainer : jade400,
                foregroundColor: active ? dangerCoral : bgAbyss,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(999),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }

  // ---------- 诊断日志 ----------

  Widget _buildLogConsole(SshRelayStatus status) {
    final List<SshLogLine> lines = status.logs;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: surfaceLowest,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: surfaceBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              Container(
                width: 7,
                height: 7,
                decoration: const BoxDecoration(
                  color: jade400,
                  shape: BoxShape.circle,
                ),
              ),
              const SizedBox(width: 6),
              const Expanded(
                child: Text(
                  'TUNNEL DIAGNOSTIC LOGS',
                  style: TextStyle(
                    color: textMuted,
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    letterSpacing: 0.8,
                  ),
                ),
              ),
              TextButton(
                onPressed: widget.onClearLogs,
                style: TextButton.styleFrom(
                  foregroundColor: textDim,
                  padding: EdgeInsets.zero,
                  minimumSize: const Size(0, 28),
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
                child: const Text('CLEAR', style: TextStyle(fontSize: 10)),
              ),
            ],
          ),
          const Divider(height: 12, color: surfaceBorder),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 168),
            child: lines.isEmpty
                ? const Padding(
                    padding: EdgeInsets.symmetric(vertical: 10),
                    child: Text(
                      '尚无日志。测试握手或建立隧道后会在这里输出实时诊断。',
                      style: TextStyle(color: textDim, fontSize: 10.5),
                    ),
                  )
                : ListView.builder(
                    reverse: true,
                    shrinkWrap: true,
                    padding: EdgeInsets.zero,
                    itemCount: lines.length,
                    itemBuilder: (BuildContext context, int index) {
                      final SshLogLine line = lines[index];
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 3),
                        child: RichText(
                          text: TextSpan(
                            style: const TextStyle(
                              fontSize: 10.5,
                              fontFamily: 'monospace',
                              height: 1.45,
                            ),
                            children: <InlineSpan>[
                              TextSpan(
                                text: '[${line.stamp}] ',
                                style: const TextStyle(color: badgeRoute),
                              ),
                              TextSpan(
                                text: line.text,
                                style: TextStyle(color: _logColor(line.level)),
                              ),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }

  Color _logColor(SshLogLevel level) {
    switch (level) {
      case SshLogLevel.ok:
        return jade400;
      case SshLogLevel.warn:
        return badgeRoute;
      case SshLogLevel.error:
        return dangerCoral;
      case SshLogLevel.info:
        return textPrimary;
    }
  }

  // ---------- 安全说明 ----------

  Widget _buildSecurityNote() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: surfaceLow,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          const Icon(Icons.verified_user_outlined, size: 18, color: badgeRoute),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              'SSH 私钥持久化于系统安全存储 (Android Keystore / iOS Keychain)；'
              '数据传输走端到端加密的反向隧道，中继主机只能看到密文。'
              '配对码是凭据，请只通过可信渠道交给要对传的设备。',
              style: TextStyle(
                color: textMuted.withValues(alpha: 0.95),
                fontSize: 10.5,
                height: 1.45,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ---------- 通用小件 ----------

  Widget _card({
    required IconData icon,
    required String title,
    Widget? trailing,
    required List<Widget> children,
  }) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: surfaceBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              Icon(icon, size: 18, color: jade400),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  title,
                  style: const TextStyle(
                    color: textPrimary,
                    fontSize: 15,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              if (trailing != null) trailing,
            ],
          ),
          const SizedBox(height: 12),
          ...children,
        ],
      ),
    );
  }

  Widget _field({
    required String label,
    required TextEditingController controller,
    String? hint,
    bool mono = false,
    bool obscure = false,
    IconData? leadingIcon,
    Widget? trailing,
    TextInputType? keyboardType,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: surfaceHigh,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            label,
            style: const TextStyle(color: textMuted, fontSize: 10.5),
          ),
          const SizedBox(height: 2),
          Row(
            children: <Widget>[
              if (leadingIcon != null) ...<Widget>[
                Icon(leadingIcon, size: 16, color: textDim),
                const SizedBox(width: 6),
              ],
              Expanded(
                child: TextField(
                  controller: controller,
                  obscureText: obscure,
                  keyboardType: keyboardType,
                  onChanged: (_) => setState(() {}),
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                    fontFamily: mono ? 'monospace' : null,
                  ),
                  decoration: InputDecoration(
                    isDense: true,
                    filled: false,
                    hintText: hint,
                    hintStyle: const TextStyle(
                      color: textDim,
                      fontSize: 12,
                      fontWeight: FontWeight.w400,
                    ),
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    contentPadding: EdgeInsets.zero,
                  ),
                ),
              ),
              if (trailing != null) trailing,
            ],
          ),
        ],
      ),
    );
  }

  Widget _dashedAction({
    required IconData icon,
    required String label,
    required bool enabled,
    required VoidCallback onTap,
  }) {
    return SizedBox(
      width: double.infinity,
      child: OutlinedButton.icon(
        onPressed: enabled ? onTap : null,
        icon: Icon(icon, size: 16),
        label: Text(label, style: const TextStyle(fontSize: 12)),
        style: OutlinedButton.styleFrom(
          foregroundColor: jade400,
          backgroundColor: surfaceHigh,
          side: const BorderSide(color: borderLuminescent),
          minimumSize: const Size(0, 42),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(10),
          ),
        ),
      ),
    );
  }

  Widget _inlineNotice({
    required IconData icon,
    required Color tone,
    required String text,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: tone.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: tone.withValues(alpha: 0.35)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Padding(
            padding: const EdgeInsets.only(top: 1),
            child: Icon(icon, size: 14, color: tone),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              text,
              style: TextStyle(color: tone, fontSize: 10.5, height: 1.4),
            ),
          ),
        ],
      ),
    );
  }
}

/// 状态指示的小圆点。
class _LiveDot extends StatelessWidget {
  const _LiveDot({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 6,
      height: 6,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}

/// 节点外扩脉冲环（对应设计稿的 `node-pulse-ring`）。
class _PulseRing extends StatefulWidget {
  const _PulseRing({required this.color});

  final Color color;

  @override
  State<_PulseRing> createState() => _PulseRingState();
}

class _PulseRingState extends State<_PulseRing>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2400),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: AnimatedBuilder(
        animation: _controller,
        builder: (BuildContext context, Widget? _) {
          final double t = _controller.value;
          final double scale = 0.9 + 0.95 * t;
          final double opacity = (1 - t) * 0.55;
          return Transform.scale(
            scale: scale,
            child: Container(
              width: 34,
              height: 34,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(
                  color: widget.color.withValues(alpha: opacity),
                ),
              ),
            ),
          );
        },
      ),
    );
  }
}

/// 链路上来回流动的光带（对应设计稿的 `particle-bullet`）。
class _ParticleTrack extends StatefulWidget {
  const _ParticleTrack({
    required this.active,
    required this.tone,
  });

  /// 隧道连通时光点提速并加亮，断开时保持缓慢流动表示"待建立"。
  final bool active;
  final Color tone;

  @override
  State<_ParticleTrack> createState() => _ParticleTrackState();
}

class _ParticleTrackState extends State<_ParticleTrack>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: _duration(),
  );

  Duration _duration() => widget.active
      ? const Duration(milliseconds: 650)
      : const Duration(milliseconds: 1800);

  @override
  void initState() {
    super.initState();
    _start();
  }

  @override
  void didUpdateWidget(_ParticleTrack oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.active != widget.active) {
      _controller.duration = _duration();
      _start();
    }
  }

  void _start() {
    _controller
      ..stop()
      ..reset()
      ..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(999),
      child: SizedBox(
        height: 2,
        child: Stack(
          children: <Widget>[
            Container(color: widget.tone.withValues(alpha: 0.18)),
            AnimatedBuilder(
              animation: _controller,
              builder: (BuildContext context, Widget? _) {
                return LayoutBuilder(
                  builder: (BuildContext context, BoxConstraints constraints) {
                    final double width = constraints.maxWidth;
                    final double t = _controller.value;
                    // 0\~30% 渐显、70%\~100% 渐隐，模拟光带穿过。
                    final double opacity = t < 0.3
                        ? t / 0.3
                        : (t > 0.7 ? (1 - t) / 0.3 : 1);
                    return Transform.translate(
                      offset: Offset((t * 2 - 1) * width, 0),
                      child: Align(
                        alignment: Alignment.centerLeft,
                        child: Opacity(
                          opacity: opacity.clamp(0.0, 1.0),
                          child: Container(
                            width: 14,
                            height: 2,
                            decoration: BoxDecoration(
                              color: widget.tone,
                              boxShadow: <BoxShadow>[
                                BoxShadow(
                                  color: widget.tone.withValues(alpha: 0.6),
                                  blurRadius: widget.active ? 10 : 5,
                                ),
                              ],
                            ),
                          ),
                        ),
                      ),
                    );
                  },
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}
