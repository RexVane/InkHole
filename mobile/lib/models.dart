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
  const ManualPeer({required this.name, required this.host});

  /// 存储格式为 `备注|主机`，备注里的竖线在写入前已被替换掉。
  factory ManualPeer.decode(String raw) {
    final int separator = raw.indexOf('|');
    if (separator < 0) return ManualPeer(name: '', host: raw.trim());
    return ManualPeer(
      name: raw.substring(0, separator).trim(),
      host: raw.substring(separator + 1).trim(),
    );
  }

  final String name;
  final String host;

  String encode() => '${name.replaceAll('|', ' ')}|$host';
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
}

/// 显示在设置里的版本号；与 pubspec.yaml 的 version 保持一致，
/// 需要时可用 `--dart-define=APP_VERSION=x.y.z` 在打包阶段覆盖。
const String appVersion =
    String.fromEnvironment('APP_VERSION', defaultValue: '2.0.2');

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
