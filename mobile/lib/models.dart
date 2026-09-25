// 页面与组件共用的纯数据模型和格式化工具。
//
// 这些类型原先散落在 home_page.dart 里；拆出来是为了让 widgets/ 下的展示组件
// 不必反向 import 页面文件。

/// 局域网/SSH 中继发现到的设备。
class PeerView {
  const PeerView({
    required this.instanceId,
    required this.name,
    required this.host,
    required this.port,
    required this.fingerprint,
    required this.serviceName,
    required this.capabilities,
  });

  factory PeerView.fromJson(Map<String, dynamic> json) {
    return PeerView(
      instanceId: json['instance_id']?.toString() ?? '',
      name: json['name']?.toString() ?? '未知设备',
      host: json['host']?.toString() ?? '',
      port: asInt(json['port']),
      fingerprint: json['fingerprint']?.toString() ?? '',
      serviceName: json['service_name']?.toString() ?? 'lan',
      capabilities: (json['capabilities'] as List<dynamic>? ?? const <dynamic>[])
          .map((dynamic value) => value.toString())
          .toList(growable: false),
    );
  }

  final String instanceId;
  final String name;
  final String host;
  final int port;
  final String fingerprint;
  final String serviceName;
  final List<String> capabilities;

  bool get viaSsh => serviceName == 'ssh' || serviceName == 'lan+ssh';

  /// 连接方式标签，依据 service_name 区分局域网直连或 SSH/QUIC 中继。
  List<String> get routes {
    switch (serviceName) {
      case 'ssh':
        return const <String>['SSH/QUIC'];
      case 'lan+ssh':
        return const <String>['局域网', 'SSH/QUIC'];
      default:
        return const <String>['局域网'];
    }
  }

  String get shortId =>
      instanceId.length > 8 ? instanceId.substring(0, 8) : instanceId;
}

/// 已落盘的接收记录。
class ReceivedFile {
  const ReceivedFile({
    required this.name,
    required this.path,
    required this.size,
    required this.receivedAt,
    required this.sender,
  });

  final String name;
  final String path;
  final int size;
  final DateTime receivedAt;
  final String sender;
}

/// 单个传输的进度快照。
class TransferProgress {
  const TransferProgress(this.filename, this.done, this.total);

  final String filename;
  final int done;
  final int total;

  double get fraction =>
      total <= 0 ? 0 : (done / total).clamp(0, 1).toDouble();
}

/// 手动添加的跨网设备（Tailscale IP / MagicDNS 主机名）。
class ManualPeer {
  const ManualPeer({required this.name, required this.host, this.port = 0});

  /// 存储格式为 `备注|主机|端口`（端口段可缺省，兼容旧格式 `备注|主机`）。
  factory ManualPeer.decode(String raw) {
    final List<String> parts = raw.split('|');
    if (parts.length == 1) return ManualPeer(name: '', host: raw.trim());
    final int port = parts.length >= 3 ? (int.tryParse(parts[2].trim()) ?? 0) : 0;
    return ManualPeer(
      name: parts[0].trim(),
      host: parts[1].trim(),
      port: (port >= 1 && port <= 65535) ? port : 0,
    );
  }

  final String name;
  final String host;

  /// 对方发现 UDP 端口；0 表示使用默认端口。
  final int port;

  String encode() => '${name.replaceAll('|', ' ')}|$host|$port';
}

/// QUIC 默认监听端口。发现信标走另一条 UDP 端口，两者不能混用。
const int quicDefaultPort = 41300;

/// 局域网发现 UDP 端口。手动地址不写端口时，核心会发到这里。
const int discoveryDefaultPort = 41301;

/// 核心在会合点留空时使用的地址。旧的 `ws://…:4000` 明文入口已不可用。
const String defaultRendezvousUrl = 'wss://relay.magic-wormhole.io/v1';

const String _legacyRendezvousUrl = 'ws://relay.magic-wormhole.io:4000/v1';

/// 空串和已废弃的明文会合点都交给核心自己填 [defaultRendezvousUrl]。
String normalizeRendezvousUrl(String raw) {
  final String value = raw.trim();
  if (value.isEmpty || value == _legacyRendezvousUrl) return '';
  return value;
}

/// 扫码端认的接收链接。裸暗号仍然可以直接输入。
String wormholeReceiveUri(String code) =>
    'inkhole://receive?code=${Uri.encodeQueryComponent(code.trim())}';

/// 把用户输入的 `主机`、`主机:端口` 或 `[IPv6]:端口` 拆开。
///
/// `41300` 是 QUIC 端口，`41301` 是发现端口的默认值。这两种写法都不再
/// 当作自定义发现端口，避免信标打到 QUIC 套接字上。
ManualPeer parseManualEndpoint(String raw, {String name = '手动节点'}) {
  final String value = raw.trim();
  String host = value;
  var port = 0;
  if (value.startsWith('[')) {
    final int end = value.indexOf(']');
    if (end > 1) {
      host = value.substring(1, end).trim();
      final String tail = value.substring(end + 1);
      if (tail.startsWith(':')) {
        port = int.tryParse(tail.substring(1).trim()) ?? 0;
      }
    }
  } else {
    final int colon = value.lastIndexOf(':');
    if (colon > 0 && !value.substring(0, colon).contains(':')) {
      final int? parsed = int.tryParse(value.substring(colon + 1).trim());
      if (parsed != null && parsed >= 1 && parsed <= 65535) {
        host = value.substring(0, colon).trim();
        port = parsed;
      }
    }
  }
  if (port == quicDefaultPort || port == discoveryDefaultPort) port = 0;
  if (port < 0 || port > 65535) port = 0;
  return ManualPeer(name: name, host: host, port: port);
}

/// 核心 `discovery_targets` 的一项。端口为 0 时只给主机，由核心补 41301。
String discoveryTargetFor(ManualPeer peer) {
  final ManualPeer parsed = parseManualEndpoint(
    peer.port > 0 ? _joinHostPort(peer.host.trim(), peer.port) : peer.host,
    name: peer.name,
  );
  if (parsed.host.isEmpty) return '';
  if (parsed.port > 0) return _joinHostPort(parsed.host, parsed.port);
  return parsed.host;
}

bool sameEndpointHost(String left, String right) {
  final String a = parseManualEndpoint(left).host.toLowerCase();
  final String b = parseManualEndpoint(right).host.toLowerCase();
  return a.isNotEmpty && a == b;
}

/// 一次性短码形如 `7-guitarist-revenge`，名字片是数字，后面至少两个词。
bool looksLikeShortCode(String raw) =>
    RegExp(r'^\d+(-[a-z0-9]+){2,}$').hasMatch(raw.trim().toLowerCase());

/// 扫到 IP 或带点的主机名时，把它当成发现目标；短码和链接返回 null。
ManualPeer? manualEndpointFromScan(String raw) {
  final String value = raw.trim();
  if (value.isEmpty || value.contains('://') || looksLikeShortCode(value)) {
    return null;
  }
  final ManualPeer peer = parseManualEndpoint(value, name: '扫码节点');
  if (!_looksLikeHost(peer.host)) return null;
  return peer;
}

bool _looksLikeHost(String host) {
  if (RegExp(r'^(\d{1,3}\.){3}\d{1,3}$').hasMatch(host)) return true;
  if (host.contains(':') && !host.contains(' ')) return true;
  return host.contains('.') && RegExp(r'^[A-Za-z0-9.-]+$').hasMatch(host);
}

/// 把节点还原成可编辑文本，端口为 0 时只给主机。
///
/// IPv6 字面量必须带方括号(`[::1]:41301`)，否则再次解析时会被当成
/// "带冒号的主机名"而整串损坏——设置页每次往返都会污染一次。
String formatManualEndpoint(ManualPeer peer) {
  if (peer.port > 0) return _joinHostPort(peer.host.trim(), peer.port);
  return peer.host.trim();
}

String _joinHostPort(String host, int port) {
  final String trimmed = host.trim();
  if (trimmed.startsWith('[') && trimmed.endsWith(']')) return '$trimmed:$port';
  if (trimmed.contains(':')) return '[$trimmed]:$port';
  return '$trimmed:$port';
}

/// 设置对话框的完整草稿，保存时整体回传给页面。
class InkSettings {
  const InkSettings({
    required this.peerName,
    required this.listenPort,
    required this.encryptionEnabled,
    required this.secret,
    required this.manualPeers,
    required this.rendezvousUrl,
    required this.transitRelay,
    required this.sshEnabled,
    required this.sshHost,
    required this.sshPort,
    required this.sshUser,
  required this.sshFingerprint,
  required this.sshPrivateKey,
  required this.sshPassphrase,
  this.sshRemotePort = 0,
});

  final String peerName;

  /// 本机监听端口，0 = 自动分配。
  final int listenPort;

  final bool encryptionEnabled;
  final String secret;
  final List<ManualPeer> manualPeers;
  final String rendezvousUrl;
  final String transitRelay;
  final bool sshEnabled;
  final String sshHost;
  final int sshPort;
  final String sshUser;
  final String sshFingerprint;
  final String sshPrivateKey;
  final String sshPassphrase;

  /// 远端监听端口(SSH 反向转发绑定的端口)。0 = 交给服务器自动分配。
  final int sshRemotePort;
}

/// 显示在设置里的版本号；与 pubspec.yaml 的 version 保持一致，
/// 需要时可用 `--dart-define=APP_VERSION=x.y.z` 在打包阶段覆盖。
const String appVersion =
    String.fromEnvironment('APP_VERSION', defaultValue: '2.1.0');

/// 旧版设置里「GitHub 仓库」指向的地址。
const String repositoryUrl = 'https://github.com/RexVane/InkHole';

int asInt(dynamic value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

String formatBytes(int bytes) {
  if (bytes <= 0) return '0 B';
  if (bytes < 1024) return '$bytes B';
  if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  if (bytes < 1024 * 1024 * 1024) {
    return '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
  }
  return '${(bytes / 1024 / 1024 / 1024).toStringAsFixed(2)} GB';
}

/// 对应旧版 InkHoleUI.kt#formatTime：刚刚 / N 分钟前 / N 小时前 / 昨天 / 具体日期。
String formatRelativeTime(DateTime time) {
  final Duration diff = DateTime.now().difference(time);
  if (diff.inMinutes < 1) return '刚刚';
  if (diff.inHours < 1) return '${diff.inMinutes} 分钟前';
  if (diff.inHours < 24) return '${diff.inHours} 小时前';
  if (diff.inHours < 48) return '昨天';
  final String hour = time.hour.toString().padLeft(2, '0');
  final String minute = time.minute.toString().padLeft(2, '0');
  return '${time.month}月${time.day}日 $hour:$minute';
}

// ==================== SSH 中继 ====================

/// SSH 中继远端监听端口的上限。0 是合法值，表示让服务器自动分配。
const int sshRemotePortAuto = 0;

/// 一个已完成 PAKE 配对的对端。
///
/// 只用于**展示身份供用户核对**——核心侧的受信列表按 `instanceId` 索引且
/// 只存在于本次中继会话内存里（每次 `ssh.listen` 都从空列表重新播种），
/// 所以这里没有"永久信任"的语义。
class SshPeerInfo {
  const SshPeerInfo({
    required this.instanceId,
    required this.name,
    required this.remotePort,
    required this.publicKey,
  });

  factory SshPeerInfo.fromJson(Map<String, dynamic> json) => SshPeerInfo(
        instanceId: json['instance_id']?.toString() ?? '',
        name: json['name']?.toString() ?? '',
        remotePort: asInt(json['remote_port']),
        publicKey: json['noise_public']?.toString() ?? '',
      );

  final String instanceId;
  final String name;
  final int remotePort;

  /// 对端的 ed25519 身份公钥（base64），用于对端数据通道握手。
  final String publicKey;

  /// 列表里显示的短标识，方便用户与对端屏幕上的字符核对。
  String get shortId => shortenIdentifier(instanceId);

  String get shortKey => shortenIdentifier(publicKey);

  String get displayName => name.trim().isEmpty ? shortId : name.trim();

  static List<SshPeerInfo> listFromJson(dynamic value) {
    if (value is! List) return const <SshPeerInfo>[];
    return value
        .whereType<Map<dynamic, dynamic>>()
        .map((Map<dynamic, dynamic> entry) =>
            SshPeerInfo.fromJson(Map<String, dynamic>.from(entry)))
        .where((SshPeerInfo peer) => peer.instanceId.isNotEmpty)
        .toList(growable: false);
  }
}

/// `ssh.check` 的结果：真实的服务器主机密钥指纹 + 服务器版本。
class SshHandshakeResult {
  const SshHandshakeResult({
    required this.fingerprint,
    required this.serverVersion,
    required this.confirmed,
    required this.elapsed,
  });

  factory SshHandshakeResult.fromJson(
    Map<String, dynamic> json, {
    required Duration elapsed,
  }) =>
      SshHandshakeResult(
        fingerprint: json['fingerprint']?.toString() ?? '',
        serverVersion: json['server_version']?.toString() ?? '',
        confirmed: json['confirmed'] == true,
        elapsed: elapsed,
      );

  final String fingerprint;
  final String serverVersion;

  /// 核心返回的指纹是否与本地已固定的指纹一致。
  final bool confirmed;

  /// 本端实测的握手往返耗时。
  ///
  /// 这是**真实测量值**（本次 `ssh.check` 的墙钟耗时，含 TCP 连接、SSH 版本
  /// 探测、认证与断开），不是从别处抄来的估算 RTT。
  final Duration elapsed;

  String get elapsedLabel => '${elapsed.inMilliseconds} ms';

  String get serverLabel =>
      serverVersion.trim().isEmpty ? '未知服务端' : serverVersion.trim();
}

/// 诊断日志的级别，决定终端里的着色。
enum SshLogLevel { info, ok, warn, error }

/// 一条诊断日志。
class SshLogLine {
  const SshLogLine({
    required this.at,
    required this.text,
    this.level = SshLogLevel.info,
  });

  final DateTime at;
  final String text;
  final SshLogLevel level;

  String get stamp => formatClock(at);
}

/// SSH 中继在 UI 侧的快照。由 home_page 持有并推给页面。
class SshRelayStatus {
  const SshRelayStatus({
    this.sessionId,
    this.boundRemotePort = 0,
    this.requestedRemotePort = 0,
    this.connected = false,
    this.starting = false,
    this.initialError = '',
    this.identityPublicKey = '',
    this.pendingPairCode = '',
    this.peers = const <SshPeerInfo>[],
    this.logs = const <SshLogLine>[],
  });

  /// 核心侧的中继会话 id；为空表示中继未启动。
  final String? sessionId;

  /// 实际绑定到的远端端口(核心回报值)。
  final int boundRemotePort;

  /// 用户请求的远端端口，0 = 自动。
  final int requestedRemotePort;

  final bool connected;
  final bool starting;

  /// 会话建立时的非致命错误(认证被拒/端口占用等)，会话本身仍然存在。
  final String initialError;

  /// 本机设备身份公钥。用于对端配对握手，**不是**可登录 VPS 的 SSH 公钥。
  final String identityPublicKey;

  /// 刚生成、等待对端输入的配对码。
  final String pendingPairCode;

  final List<SshPeerInfo> peers;
  final List<SshLogLine> logs;

  bool get active => sessionId != null;

  /// 展示用的远端端口：优先显示核心实际绑定到的端口。
  int get displayRemotePort =>
      boundRemotePort != 0 ? boundRemotePort : requestedRemotePort;

  SshRelayStatus copyWith({
    Object? sessionId = _unset,
    int? boundRemotePort,
    int? requestedRemotePort,
    bool? connected,
    bool? starting,
    String? initialError,
    String? identityPublicKey,
    String? pendingPairCode,
    List<SshPeerInfo>? peers,
    List<SshLogLine>? logs,
  }) =>
      SshRelayStatus(
        sessionId: identical(sessionId, _unset)
            ? this.sessionId
            : sessionId as String?,
        boundRemotePort: boundRemotePort ?? this.boundRemotePort,
        requestedRemotePort: requestedRemotePort ?? this.requestedRemotePort,
        connected: connected ?? this.connected,
        starting: starting ?? this.starting,
        initialError: initialError ?? this.initialError,
        identityPublicKey: identityPublicKey ?? this.identityPublicKey,
        pendingPairCode: pendingPairCode ?? this.pendingPairCode,
        peers: peers ?? this.peers,
        logs: logs ?? this.logs,
      );
}

/// `copyWith` 里区分"没传"和"显式传 null"的哨兵。
const Object _unset = Object();

/// SSH 中继配置校验；返回 null 表示通过，否则返回给用户看的原因。
///
/// 逐条对齐核心 `SshProfile::normalize` 与 `valid_host_fingerprint` 的要求，
/// 目的是在前端就拦掉、而不是等一次往返之后拿一句
/// "SSH host, user and a valid private key are required"。
///
/// [requireFingerprint] 为 false 时跳过指纹检查：**「测试握手」必须能跑**，
/// 因为指纹正是那次握手的结果（核心 `ssh.check` 走 `normalize(false)`，
/// 本身不要求指纹）。只有真正建立中继时才必须已固定指纹，否则
/// `validate_relay()` 会直接拒绝启动。
String? validateSshRelayConfig({
  required String host,
  required int port,
  required String user,
  required String fingerprint,
  required String privateKey,
  required int remotePort,
  bool requireFingerprint = true,
}) {
  final String trimmedHost = host.trim();
  if (trimmedHost.isEmpty) return '请填写服务器地址';
  if (trimmedHost.length > 255) return '服务器地址过长';
  if (trimmedHost.contains(RegExp(r'\s'))) return '服务器地址不能包含空格';
  if (port < 1 || port > 65535) return 'SSH 端口必须在 1-65535 范围内';
  final String trimmedUser = user.trim();
  if (trimmedUser.isEmpty) return '请填写登录用户';
  if (trimmedUser.length > 255) return '登录用户过长';
  if (privateKey.trim().isEmpty) return '请导入 SSH 私钥';
  if (remotePort < 0 || remotePort > 65535) return '远端端口必须在 0-65535 范围内';
  // 中继必须固定主机指纹(核心 validate_relay → normalize(true))，
  // 空指纹会被直接拒绝启动，所以这里不能放行。
  if (requireFingerprint && freshHostFingerprint(fingerprint) == null) {
    return '请先「测试握手」取得并确认主机指纹';
  }
  return null;
}

/// 校验并规范化 `SHA256:...` 主机指纹；不合法返回 null。
String? freshHostFingerprint(String value) {
  final String trimmed = value.trim();
  final String? encoded =
      trimmed.startsWith('SHA256:') ? trimmed.substring('SHA256:'.length) : null;
  if (encoded == null) return null;
  if (encoded.length < 32 || encoded.length > 64) return null;
  final bool valid = encoded.codeUnits.every((int unit) {
    final bool alnum = (unit >= 0x30 && unit <= 0x39) ||
        (unit >= 0x41 && unit <= 0x5A) ||
        (unit >= 0x61 && unit <= 0x7A);
    return alnum ||
        unit == 0x2B ||
        unit == 0x2F ||
        unit == 0x3D ||
        unit == 0x5F ||
        unit == 0x2D;
  });
  return valid ? trimmed : null;
}

/// `HH:MM:SS`，诊断日志的时间戳。
String formatClock(DateTime at) {
  String two(int value) => value.toString().padLeft(2, '0');
  return '${two(at.hour)}:${two(at.minute)}:${two(at.second)}';
}

/// 把一个长标识压成 `前 head 位…后 tail 位`，便于在窄屏上核对身份。
String shortenIdentifier(String value, {int head = 10, int tail = 6}) {
  final String trimmed = value.trim();
  if (trimmed.isEmpty) return '';
  if (trimmed.length <= head + tail + 1) return trimmed;
  return '${trimmed.substring(0, head)}…'
      '${trimmed.substring(trimmed.length - tail)}';
}

