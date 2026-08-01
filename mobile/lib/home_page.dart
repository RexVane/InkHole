import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/inkhole_core.dart';

const _background = Color(0xff080e13);
const _surface = Color(0xff101a21);
const _surfaceRaised = Color(0xff17242b);
const _teal = Color(0xff58e6c8);
const _tealDim = Color(0xff2c9d8b);
const _text = Color(0xffe7f1f0);
const _muted = Color(0xff8da4a6);
const _dim = Color(0xff5e7478);

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
      port: _asInt(json['port']),
      fingerprint: json['fingerprint']?.toString() ?? '',
      serviceName: json['service_name']?.toString() ?? 'lan',
      capabilities: (json['capabilities'] as List<dynamic>? ?? const [])
          .map((value) => value.toString())
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
  String get shortId => instanceId.length > 8 ? instanceId.substring(0, 8) : instanceId;
}

class ReceivedFile {
  const ReceivedFile({
    required this.name,
    required this.path,
    required this.size,
    required this.receivedAt,
  });

  final String name;
  final String path;
  final int size;
  final DateTime receivedAt;
}

class _Progress {
  const _Progress(this.filename, this.done, this.total);

  final String filename;
  final int done;
  final int total;

  double get fraction => total <= 0 ? 0 : (done / total).clamp(0, 1).toDouble();
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with SingleTickerProviderStateMixin {
  static const MethodChannel _shareChannel = MethodChannel('com.rexvane.inkhole/share');

  final _secureStorage = const FlutterSecureStorage();
  late final AnimationController _heroAnimation;
  late final InkHoleCore _core;
  StreamSubscription<Map<String, dynamic>>? _events;

  SharedPreferences? _preferences;
  String? _sessionId;
  String? _identityPrivate;
  String _peerName = 'Android';
  String _inbox = '';
  String _sshHost = '';
  String _sshUser = '';
  String _sshFingerprint = '';
  int _sshPort = 22;
  bool _sshEnabled = false;
  String? _sshSessionId;
  String _status = '正在启动 Rust 核心…';
  String? _error;
  String? _selectedInstance;
  String? _activeWormholeSession;
  bool _starting = true;
  bool _sending = false;
  final ValueNotifier<bool> _joining = ValueNotifier<bool>(false);
  final ValueNotifier<String?> _joinError = ValueNotifier<String?>(null);
  bool _encryptionEnabled = false;
  List<PeerView> _peers = const [];
  final List<ReceivedFile> _received = <ReceivedFile>[];
  final List<String> _sharedFiles = <String>[];
  final Map<String, _Progress> _progress = <String, _Progress>{};

  @override
  void initState() {
    super.initState();
    _heroAnimation = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 9),
    )..repeat();
    _core = InkHoleCore();
    _shareChannel.setMethodCallHandler(_onShareMethodCall);
    unawaited(_loadSharedFiles());
    unawaited(_boot());
  }

  @override
  void dispose() {
    _heroAnimation.dispose();
    _events?.cancel();
    _joining.dispose();
    _joinError.dispose();
    _shareChannel.setMethodCallHandler(null);
    unawaited(_stopCore());
    super.dispose();
  }

  Future<void> _loadSharedFiles() async {
    try {
      final paths = await _shareChannel.invokeListMethod<String>('consumeSharedFiles') ?? const [];
      _queueSharedFiles(paths);
    } catch (_) {
      // The channel is Android-only; other Flutter targets simply use the picker.
    }
  }

  Future<dynamic> _onShareMethodCall(MethodCall call) async {
    if (call.method == 'sharedFiles' && call.arguments is List) {
      _queueSharedFiles((call.arguments as List).whereType<String>());
    }
    return null;
  }

  void _queueSharedFiles(Iterable<String> paths) {
    final fresh = paths
        .map((path) => path.trim())
        .where((path) => path.isNotEmpty && !_sharedFiles.contains(path))
        .toList(growable: false);
    if (fresh.isEmpty || !mounted) return;
    setState(() => _sharedFiles.addAll(fresh));
    _setStatus('System share ready (${_sharedFiles.length})');
  }

  Future<void> _boot() async {
    try {
      _preferences = await SharedPreferences.getInstance();
      _peerName = _preferences!.getString('peer_name') ?? Platform.localHostname;
      if (_peerName.trim().isEmpty) _peerName = 'Android';
      _inbox = await _resolveInbox();
      _encryptionEnabled = _preferences!.getBool('encryption_enabled') ?? false;
      // The export contains the device TLS and signing private keys; keep it
      // in platform secure storage instead of ordinary app preferences.
      _identityPrivate = await _secureStorage.read(key: 'identity_private');
      final legacyIdentity = _preferences!.getString('identity_private');
      if (_identityPrivate == null || _identityPrivate!.trim().isEmpty) {
        if (legacyIdentity != null && legacyIdentity.trim().isNotEmpty) {
          await _secureStorage.write(key: 'identity_private', value: legacyIdentity);
          _identityPrivate = legacyIdentity;
          await _preferences!.remove('identity_private');
        }
      } else if (legacyIdentity != null) {
        await _preferences!.remove('identity_private');
      }
      await _loadSshSettings();
      _events = _core.events.listen(_onCoreEvent);
      await _core.start();
      final result = await _core.call('lan.start', <String, dynamic>{
        'peer_name': _peerName,
        'instance_id': _loadInstanceId(),
        'identity_private': _identityPrivate ?? '',
        'secret': _encryptionEnabled
            ? await _secureStorage.read(key: 'transfer_secret') ?? ''
            : '',
        'inbox': _inbox,
        'inbox_category_roots': <String, dynamic>{},
        'listen_port': 0,
        'capabilities': const ['quic-v2', 'blake3', 'folder-v1'],
        'discovery_targets': const <String>[],
      });
      _sessionId = result['session_id']?.toString();
      _identityPrivate = result['identity_private']?.toString();
      if (_identityPrivate != null) {
        await _secureStorage.write(
          key: 'identity_private',
          value: _identityPrivate!,
        );
      }
      if (_sshEnabled) unawaited(_startSsh());
      if (!mounted) return;
      setState(() {
        _starting = false;
        _status = '等待附近的墨洞上线';
      });
      if (_sharedFiles.isNotEmpty) {
        _setStatus('System share ready (${_sharedFiles.length})');
      }
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _starting = false;
        _error = error.toString();
        _status = 'Rust 核心暂不可用';
      });
    }
  }

  Future<void> _loadSshSettings() async {
    _sshHost = _preferences!.getString('ssh_host') ?? '';
    _sshUser = _preferences!.getString('ssh_user') ?? '';
    _sshFingerprint = _preferences!.getString('ssh_fingerprint') ?? '';
    _sshPort = _preferences!.getInt('ssh_port') ?? 22;
    _sshEnabled = _preferences!.getBool('ssh_enabled') ?? false;
  }

  Future<Map<String, dynamic>> _sshProfile({
    String? privateKey,
    String? passphrase,
  }) async {
    return <String, dynamic>{
      'id': 'mobile-default',
      'host': _sshHost,
      'port': _sshPort,
      'user': _sshUser,
      'private_key': privateKey ?? await _secureStorage.read(key: 'ssh_private_key') ?? '',
      'private_key_label': 'Flutter secure storage',
      'passphrase': passphrase ?? await _secureStorage.read(key: 'ssh_passphrase') ?? '',
      'host_key_sha256': _sshFingerprint,
    };
  }

  Future<void> _startSsh() async {
    if (_sessionId == null || !_sshEnabled) return;
    final profile = await _sshProfile();
    if (profile['host'].toString().trim().isEmpty ||
        profile['user'].toString().trim().isEmpty ||
        profile['private_key'].toString().trim().isEmpty ||
        profile['host_key_sha256'].toString().trim().isEmpty) {
      _setStatus('SSH 设置不完整，请先验证主机指纹');
      return;
    }
    try {
      final result = await _core.call('ssh.listen', <String, dynamic>{
        'session_id': _sessionId,
        'profile': profile,
        'remote_port': 0,
        'peers': const <Map<String, dynamic>>[],
      });
      _sshSessionId = result['session_id']?.toString();
      _setStatus(result['connected'] == true ? 'SSH 中继已连接' : 'SSH 中继正在重连');
    } catch (error) {
      _setStatus('SSH 中继启动失败：$error');
    }
  }

  Future<String> _resolveInbox() async {
    final root = await getApplicationDocumentsDirectory();
    final inbox = Directory(p.join(root.path, 'InkHole'));
    await inbox.create(recursive: true);
    return inbox.path;
  }

  String _loadInstanceId() {
    final existing = _preferences!.getString('instance_id');
    if (existing != null && RegExp(r'^[a-f0-9]{32}$').hasMatch(existing)) return existing;
    final random = Random.secure();
    final id = List<String>.generate(
      32,
      (_) => random.nextInt(16).toRadixString(16),
    ).join();
    unawaited(_preferences!.setString('instance_id', id));
    return id;
  }

  Future<void> _stopCore() async {
    if (_sshSessionId != null) {
      try {
        await _core.call('session.cancel', <String, dynamic>{'session_id': _sshSessionId});
      } catch (_) {}
    }
    if (_sessionId != null) {
      try {
        await _core.call('lan.stop', <String, dynamic>{'session_id': _sessionId});
      } catch (_) {}
    }
    await _core.dispose();
  }

  void _onCoreEvent(Map<String, dynamic> event) {
    final name = event['event']?.toString() ?? '';
    final data = Map<String, dynamic>.from(event['data'] as Map? ?? const {});
    switch (name) {
      case 'core.fatal':
        if (!mounted) return;
        setState(() {
          _error = data['message']?.toString() ?? '共享传输核心已停止';
          _status = 'Rust 核心已停止';
          _sending = false;
          _progress.clear();
        });
        _joining.value = false;
      case 'lan.peers':
        final values = data['peers'] as List<dynamic>? ?? const [];
        if (!mounted) return;
        setState(() {
          _peers = values
              .whereType<Map>()
              .map((value) => PeerView.fromJson(Map<String, dynamic>.from(value)))
              .toList(growable: false);
          if (_selectedInstance != null &&
              !_peers.any((peer) => peer.instanceId == _selectedInstance)) {
            _selectedInstance = null;
          }
          if (_peers.isNotEmpty && _status == '等待附近的墨洞上线') {
            _status = '发现 ${_peers.length} 台设备';
          }
        });
      case 'lan.status':
        _setStatus(data['message']?.toString() ?? '局域网状态已更新');
      case 'lan.progress':
        final key = data['send_id']?.toString() ?? data['transfer_id']?.toString() ?? name;
        final progress = _Progress(
          data['filename']?.toString() ?? '文件',
          _asInt(data['done']),
          _asInt(data['total']),
        );
        if (!mounted) return;
        setState(() => _progress[key] = progress);
      case 'lan.sent':
        final id = data['send_id']?.toString();
        if (!mounted) return;
        setState(() {
          if (id != null) _progress.remove(id);
          _sending = _progress.isNotEmpty;
        });
        _setStatus(data['ok'] == true ? '发送完成' : '发送失败：${data['error'] ?? '连接中断'}');
      case 'lan.received':
        final path = data['path']?.toString() ?? '';
        final receiveId = data['send_id']?.toString() ?? data['transfer_id']?.toString();
        if (!mounted) return;
        setState(() {
          if (receiveId != null) _progress.remove(receiveId);
          _sending = _progress.isNotEmpty;
          _received.insert(
            0,
            ReceivedFile(
              name: data['filename']?.toString() ?? p.basename(path),
              path: path,
              size: _asInt(data['size']),
              receivedAt: DateTime.now(),
            ),
          );
        });
        _setStatus('已接收文件');
      case 'wormhole.offer':
        _activeWormholeSession = data['session_id']?.toString();
        if (mounted) unawaited(_showWormholeOffer(data));
      case 'wormhole.ready':
        _joining.value = false;
        _joinError.value = null;
        _setStatus('跨网络传输已连接');
      case 'wormhole.error':
        _joining.value = false;
        _activeWormholeSession = null;
        final reason = data['error']?.toString() ?? '连接中断';
        _joinError.value = reason;
        _setStatus('短码传输失败：$reason');
      case 'ssh.ready':
        _setStatus(data['connected'] == true ? 'SSH 中继已连接' : 'SSH 中继正在重连');
      case 'ssh.connected':
        _setStatus('SSH 中继已恢复');
      case 'ssh.disconnected':
        _setStatus('SSH 中继已断开，正在重连');
      case 'ssh.data.error':
      case 'ssh.config.error':
      case 'ssh.pair.error':
        _setStatus(data['error']?.toString() ?? 'SSH 中继异常');
    }
  }

  Future<void> _refresh() async {
    if (_sessionId == null) return;
    try {
      await _core.call('lan.refresh', <String, dynamic>{'session_id': _sessionId});
      _setStatus('正在搜索附近的墨洞');
    } catch (error) {
      _setStatus('刷新失败：$error');
    }
  }

  Future<void> _chooseAndSend() async {
    final selected = _peers.where((peer) => peer.instanceId == _selectedInstance).firstOrNull;
    if (selected == null) {
      _setStatus('请先选择目标设备');
      return;
    }
    if (_sessionId == null) return;
    if (_sharedFiles.isNotEmpty) {
      final sharedPaths = _sharedFiles.toList(growable: false);
      setState(() => _sharedFiles.clear());
      await _sendPaths(sharedPaths, selected);
      return;
    }
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: false,
    );
    if (result == null || _sessionId == null) return;
    for (final file in result.files) {
      final path = file.path;
      if (path == null || path.isEmpty) continue;
      try {
        final response = await _core.call('lan.send', <String, dynamic>{
          'session_id': _sessionId,
          'path': path,
          'instance_id': selected.instanceId,
          'host': selected.host,
          'port': selected.port,
          'fingerprint': selected.fingerprint,
          'endpoint_token': '',
        });
        final id = response['send_id']?.toString();
        if (id != null && mounted) setState(() => _sending = true);
      } catch (error) {
        _setStatus('发送失败：$error');
      }
    }
  }

  Future<void> _sendPaths(List<String> paths, PeerView selected) async {
    for (final path in paths) {
      try {
        final response = await _core.call('lan.send', <String, dynamic>{
          'session_id': _sessionId,
          'path': path,
          'instance_id': selected.instanceId,
          'host': selected.host,
          'port': selected.port,
          'fingerprint': selected.fingerprint,
          'endpoint_token': '',
        });
        final id = response['send_id']?.toString();
        if (id != null && mounted) setState(() => _sending = true);
      } catch (error) {
        _setStatus('System share send failed: $error');
      }
    }
  }

  Future<void> _cancelSending() async {
    if (_sessionId == null) return;
    final sends = _progress.keys.toList(growable: false);
    for (final sendId in sends) {
      try {
        await _core.call('lan.send.cancel', <String, dynamic>{
          'session_id': _sessionId,
          'send_id': sendId,
        });
      } catch (_) {}
    }
    if (mounted) setState(() => _sending = false);
    _setStatus('已取消发送');
  }

  Future<void> _createOneTime() async {
    final paths = await _pickPaths();
    if (paths.isEmpty || _sessionId == null) return;
    try {
      final result = await _core.call('wormhole.create', <String, dynamic>{
        'session_id': _sessionId,
        'paths': paths,
        'settings': _wormholeSettings(),
      });
      _activeWormholeSession = result['session_id']?.toString();
      if (mounted) await _showShortCode(result, title: '发送短码');
    } catch (error) {
      _setStatus('无法生成短码：$error');
    }
  }

  Future<void> _joinOneTime(String code) async {
    if (_sessionId == null || code.trim().isEmpty) return;
    _joinError.value = null;
    _joining.value = true;
    try {
      final result = await _core.call('wormhole.join.start', <String, dynamic>{
        'session_id': _sessionId,
        'code': code.trim(),
        'settings': _wormholeSettings(),
      });
      _activeWormholeSession = result['session_id']?.toString();
      if (mounted) Navigator.of(context).pop();
      _setStatus('等待发送端确认内容');
    } catch (error) {
      _joining.value = false;
      _joinError.value = '短码无效：$error';
      _setStatus('短码无效：$error');
    }
  }

  Map<String, dynamic> _wormholeSettings() => <String, dynamic>{
        'rendezvous_url': '',
        'transit_relay': '',
        'timeout_minutes': 10,
      };

  Future<List<String>> _pickPaths() async {
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: false,
    );
    return result?.files
            .map((file) => file.path)
            .whereType<String>()
            .where((path) => path.isNotEmpty)
            .toList(growable: false) ??
        const [];
  }

  Future<void> _showCrossNetwork() async {
    final codeController = TextEditingController();
    _joinError.value = null;
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: _surface,
        title: const Text('跨网络传输'),
        content: StatefulBuilder(
          builder: (context, setDialogState) => SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                FilledButton.icon(
                  onPressed: _starting ? null : _createOneTime,
                  icon: const Icon(Icons.send_outlined),
                  label: const Text('选择内容并生成短码'),
                ),
                const SizedBox(height: 12),
                ValueListenableBuilder<bool>(
                  valueListenable: _joining,
                  builder: (context, joining, _) => Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      TextField(
                        controller: codeController,
                        enabled: !joining,
                        decoration: const InputDecoration(
                          labelText: '一次性接收短码',
                          prefixIcon: Icon(Icons.password_outlined),
                        ),
                        onChanged: (_) => setDialogState(() {}),
                      ),
                      const SizedBox(height: 8),
                      OutlinedButton.icon(
                        onPressed: codeController.text.trim().isEmpty || joining
                            ? null
                            : () => _joinOneTime(codeController.text),
                        icon: const Icon(Icons.download_outlined),
                        label: Text(joining ? '正在连接…' : '连接并接收'),
                      ),
                    ],
                  ),
                ),
                ValueListenableBuilder<String?>(
                  valueListenable: _joinError,
                  builder: (context, message, _) => message == null
                      ? const SizedBox.shrink()
                      : Padding(
                          padding: const EdgeInsets.only(top: 8),
                          child: Align(
                            alignment: Alignment.centerLeft,
                            child: Text(
                              message,
                              style: const TextStyle(
                                color: Colors.redAccent,
                                fontSize: 12,
                              ),
                            ),
                          ),
                        ),
                ),
                const Divider(height: 26),
                const Align(
                  alignment: Alignment.centerLeft,
                  child: Text('SSH 中继配对', style: TextStyle(fontWeight: FontWeight.w600)),
                ),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  onPressed: _sshSessionId == null ? null : _createSshPair,
                  icon: const Icon(Icons.key_outlined),
                  label: const Text('生成配对码'),
                ),
                TextField(
                  decoration: const InputDecoration(labelText: 'SSH 配对码'),
                  onChanged: (value) => setDialogState(() => _sshPairCode = value),
                ),
                OutlinedButton(
                  onPressed: _sshPairCode.trim().isEmpty ? null : _joinSshPair,
                  child: const Text('加入配对'),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialogContext).pop(), child: const Text('关闭')),
        ],
      ),
    );
    codeController.dispose();
  }

  String _sshPairCode = '';

  Future<void> _createSshPair() async {
    if (_sshSessionId == null) return;
    try {
      final result = await _core.call('ssh.pair.create', <String, dynamic>{
        'session_id': _sshSessionId,
      });
      if (mounted) await _showShortCode(result, title: 'SSH 配对码');
    } catch (error) {
      _setStatus('SSH 配对不可用：$error');
    }
  }

  Future<void> _joinSshPair() async {
    if (_sshSessionId == null || _sshPairCode.trim().isEmpty) return;
    try {
      await _core.call('ssh.pair.join', <String, dynamic>{
        'session_id': _sshSessionId,
        'code': _sshPairCode.trim(),
      });
      _setStatus('SSH 设备已加入');
    } catch (error) {
      _setStatus('SSH 配对失败：$error');
    }
  }

  Future<void> _showShortCode(
    Map<String, dynamic> result, {
    required String title,
  }) async {
    if (!mounted) return;
    final code = result['code']?.toString() ?? '';
    final uri = result['uri']?.toString() ?? code;
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: _surface,
        title: Text(title),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (uri.isNotEmpty)
                QrImageView(
                  data: uri,
                  size: min(MediaQuery.sizeOf(context).width * .58, 220),
                  eyeStyle: const QrEyeStyle(
                    eyeShape: QrEyeShape.square,
                    color: _text,
                  ),
                  dataModuleStyle: const QrDataModuleStyle(
                    dataModuleShape: QrDataModuleShape.square,
                    color: _text,
                  ),
                  backgroundColor: _surface,
                ),
              const SizedBox(height: 12),
              SelectableText(
                code,
                textAlign: TextAlign.center,
                style: const TextStyle(fontFamily: 'monospace', fontWeight: FontWeight.w600),
              ),
              if (result['expires_at'] != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    '有效期至 ${result['expires_at']}',
                    style: const TextStyle(color: _muted, fontSize: 12),
                  ),
                ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('关闭')),
        ],
      ),
    );
  }

  Future<void> _showWormholeOffer(Map<String, dynamic> data) async {
    if (!mounted) return;
    final summary = Map<String, dynamic>.from(data['summary'] as Map? ?? const {});
    final names = (summary['names'] as List<dynamic>? ?? const [])
        .map((value) => value.toString())
        .toList(growable: false);
    final session = data['session_id']?.toString() ?? _activeWormholeSession;
    final accept = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (context) => AlertDialog(
        backgroundColor: _surface,
        title: const Text('接收一次性传输'),
        content: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(summary['device_name']?.toString() ?? '未知设备'),
              const SizedBox(height: 8),
              Text(
                '${summary['item_count'] ?? 0} 项 · ${_formatBytes(_asInt(summary['total_bytes']))}',
                style: const TextStyle(color: _muted, fontSize: 12),
              ),
              if (names.isNotEmpty) ...[
                const SizedBox(height: 12),
                ...names.map((name) => Text(name, style: const TextStyle(fontSize: 12))),
              ],
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('拒绝')),
          FilledButton(onPressed: () => Navigator.of(context).pop(true), child: const Text('接收')),
        ],
      ),
    );
    if (session == null) return;
    try {
      await _core.call(
        accept == true ? 'wormhole.accept' : 'wormhole.reject',
        <String, dynamic>{'session_id': session},
      );
    } catch (error) {
      _setStatus('短码确认失败：$error');
    }
  }

  Future<void> _openSettings() async {
    final nameController = TextEditingController(text: _peerName);
    final secretController = TextEditingController(
      text: _encryptionEnabled
          ? await _secureStorage.read(key: 'transfer_secret') ?? ''
          : '',
    );
    final sshHostController = TextEditingController(text: _sshHost);
    final sshPortController = TextEditingController(text: _sshPort.toString());
    final sshUserController = TextEditingController(text: _sshUser);
    final sshFingerprintController = TextEditingController(text: _sshFingerprint);
    final sshKeyController = TextEditingController(
      text: await _secureStorage.read(key: 'ssh_private_key') ?? '',
    );
    final sshPassphraseController = TextEditingController(
      text: await _secureStorage.read(key: 'ssh_passphrase') ?? '',
    );
    var encryption = _encryptionEnabled;
    var sshEnabled = _sshEnabled;
    if (!mounted) {
      nameController.dispose();
      secretController.dispose();
      sshHostController.dispose();
      sshPortController.dispose();
      sshUserController.dispose();
      sshFingerprintController.dispose();
      sshKeyController.dispose();
      sshPassphraseController.dispose();
      return;
    }
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          backgroundColor: _surface,
          title: const Text('设置'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: nameController,
                  maxLength: 40,
                  decoration: const InputDecoration(labelText: '设备名称'),
                ),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  value: encryption,
                  onChanged: (value) => setDialogState(() => encryption = value),
                  title: const Text('端到端加密'),
                  subtitle: const Text('使用共享口令保护局域网内容'),
                ),
                if (encryption)
                  TextField(
                    controller: secretController,
                    obscureText: true,
                    decoration: const InputDecoration(labelText: '共享口令'),
                  ),
                const Divider(height: 28),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  value: sshEnabled,
                  onChanged: (value) => setDialogState(() => sshEnabled = value),
                  title: const Text('启用 SSH VPS 中继'),
                  subtitle: const Text('需要先验证主机指纹'),
                ),
                TextField(
                  controller: sshHostController,
                  decoration: const InputDecoration(labelText: 'SSH 主机'),
                ),
                Row(
                  children: [
                    SizedBox(
                      width: 100,
                      child: TextField(
                        controller: sshPortController,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(labelText: '端口'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: TextField(
                        controller: sshUserController,
                        decoration: const InputDecoration(labelText: 'SSH 用户'),
                      ),
                    ),
                  ],
                ),
                TextField(
                  controller: sshKeyController,
                  autocorrect: false,
                  minLines: 1,
                  maxLines: 3,
                  decoration: const InputDecoration(labelText: 'OpenSSH / PEM 私钥'),
                ),
                TextField(
                  controller: sshPassphraseController,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: '私钥口令（可选）'),
                ),
                TextField(
                  controller: sshFingerprintController,
                  decoration: const InputDecoration(labelText: '主机指纹 SHA256:…'),
                ),
                Align(
                  alignment: Alignment.centerLeft,
                  child: OutlinedButton.icon(
                    onPressed: () async {
                      final host = sshHostController.text.trim();
                      final user = sshUserController.text.trim();
                      final key = sshKeyController.text.trim();
                      final port = int.tryParse(sshPortController.text.trim()) ?? 22;
                      if (host.isEmpty || user.isEmpty || key.isEmpty || port <= 0 || port > 65535) {
                        _setStatus('请完整填写 SSH 主机、端口、用户和私钥');
                        return;
                      }
                      try {
                        final result = await _core.call('ssh.check', <String, dynamic>{
                          'profile': <String, dynamic>{
                            'id': 'mobile-default',
                            'host': host,
                            'port': port,
                            'user': user,
                            'private_key': key,
                            'private_key_label': 'Flutter secure storage',
                            'passphrase': sshPassphraseController.text,
                            'host_key_sha256': sshFingerprintController.text.trim(),
                          },
                        });
                        final fingerprint = result['fingerprint']?.toString() ?? '';
                        if (fingerprint.isNotEmpty) {
                          setDialogState(() => sshFingerprintController.text = fingerprint);
                        }
                        if (context.mounted) {
                          await showDialog<void>(
                            context: context,
                            builder: (context) => AlertDialog(
                              backgroundColor: _surface,
                              title: const Text('确认 VPS 主机指纹'),
                              content: SelectableText(fingerprint),
                              actions: [
                                TextButton(
                                  onPressed: () => Navigator.of(context).pop(),
                                  child: const Text('知道了'),
                                ),
                              ],
                            ),
                          );
                        }
                      } catch (error) {
                        _setStatus('SSH 检查失败：$error');
                      }
                    },
                    icon: const Icon(Icons.verified_user_outlined, size: 17),
                    label: const Text('检查主机指纹'),
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  '收件箱：$_inbox',
                  style: const TextStyle(color: _muted, fontSize: 11),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.of(dialogContext).pop(), child: const Text('取消')),
            FilledButton(
              onPressed: () async {
                final name = nameController.text.trim();
                if (name.isEmpty) return;
                final saved = await _saveSettings(
                  name,
                  encryption,
                  secretController.text,
                  sshEnabled: sshEnabled,
                  sshHost: sshHostController.text,
                  sshPort: int.tryParse(sshPortController.text) ?? 22,
                  sshUser: sshUserController.text,
                  sshFingerprint: sshFingerprintController.text,
                  sshPrivateKey: sshKeyController.text,
                  sshPassphrase: sshPassphraseController.text,
                );
                if (saved && dialogContext.mounted) Navigator.of(dialogContext).pop();
              },
              child: const Text('保存'),
            ),
          ],
        ),
      ),
    );
    nameController.dispose();
    secretController.dispose();
    sshHostController.dispose();
    sshPortController.dispose();
    sshUserController.dispose();
    sshFingerprintController.dispose();
    sshKeyController.dispose();
    sshPassphraseController.dispose();
  }

  Future<bool> _saveSettings(
    String name,
    bool encryption,
    String secret, {
    required bool sshEnabled,
    required String sshHost,
    required int sshPort,
    required String sshUser,
    required String sshFingerprint,
    required String sshPrivateKey,
    required String sshPassphrase,
  }) async {
    final normalizedSecret = secret.trim();
    if (encryption && normalizedSecret.isEmpty) {
      final existingSecret = await _secureStorage.read(key: 'transfer_secret');
      if (existingSecret == null || existingSecret.trim().isEmpty) {
        _setStatus('启用端到端加密前请先设置共享口令');
        return false;
      }
    }
    await _preferences!.setString('peer_name', name);
    await _preferences!.setBool('encryption_enabled', encryption);
    if (encryption && normalizedSecret.isNotEmpty) {
      await _secureStorage.write(key: 'transfer_secret', value: normalizedSecret);
    } else if (!encryption) {
      await _secureStorage.delete(key: 'transfer_secret');
    }
    await _preferences!.setBool('ssh_enabled', sshEnabled);
    await _preferences!.setString('ssh_host', sshHost.trim());
    final normalizedSshPort = sshPort.clamp(1, 65535).toInt();
    await _preferences!.setInt('ssh_port', normalizedSshPort);
    await _preferences!.setString('ssh_user', sshUser.trim());
    await _preferences!.setString('ssh_fingerprint', sshFingerprint.trim());
    if (sshPrivateKey.trim().isNotEmpty) {
      await _secureStorage.write(key: 'ssh_private_key', value: sshPrivateKey.trim());
    }
    if (sshPassphrase.isNotEmpty) {
      await _secureStorage.write(key: 'ssh_passphrase', value: sshPassphrase);
    }
    if (!sshEnabled && _sshSessionId != null) {
      try {
        await _core.call('session.cancel', <String, dynamic>{'session_id': _sshSessionId});
      } catch (_) {}
      _sshSessionId = null;
    }
    if (!mounted) return true;
    setState(() {
      _peerName = name;
      _encryptionEnabled = encryption;
      _sshEnabled = sshEnabled;
      _sshHost = sshHost.trim();
      _sshPort = normalizedSshPort;
      _sshUser = sshUser.trim();
      _sshFingerprint = sshFingerprint.trim();
    });
    if (sshEnabled) unawaited(_startSsh());
    _setStatus('设置已保存，重启后生效');
    return true;
  }

  void _setStatus(String value) {
    if (mounted) setState(() => _status = value);
  }

  Future<void> _openInbox() async {
    _setStatus('收件箱位于 $_inbox');
  }

  void _clearHistory() {
    setState(() => _received.clear());
  }

  double get _transferFraction {
    if (_progress.isEmpty) return -1;
    final values = _progress.values.toList(growable: false);
    final total = values.fold<int>(0, (sum, item) => sum + item.total);
    final done = values.fold<int>(0, (sum, item) => sum + item.done);
    return total <= 0 ? 0 : (done / total).clamp(0, 1).toDouble();
  }

  @override
  Widget build(BuildContext context) {
    final selected = _peers.where((peer) => peer.instanceId == _selectedInstance).firstOrNull;
    return Scaffold(
      backgroundColor: _background,
      appBar: AppBar(
        backgroundColor: _background,
        elevation: 0,
        titleSpacing: 18,
        title: const Text(
          '墨洞',
          style: TextStyle(color: _text, fontSize: 19, fontWeight: FontWeight.w700),
        ),
        actions: [
          IconButton(
            tooltip: '刷新设备',
            onPressed: _starting ? null : _refresh,
            icon: const Icon(Icons.refresh_rounded),
          ),
          IconButton(
            tooltip: '跨网络传输',
            onPressed: _starting ? null : _showCrossNetwork,
            icon: const Icon(Icons.public_rounded),
          ),
          IconButton(
            tooltip: '设置',
            onPressed: _openSettings,
            icon: const Icon(Icons.tune_rounded),
          ),
          const SizedBox(width: 6),
        ],
      ),
      body: LayoutBuilder(
        builder: (context, constraints) {
          final heroSize = min(max(constraints.maxHeight * .34, 150), 240).toDouble();
          return Padding(
            padding: const EdgeInsets.symmetric(horizontal: 18),
            child: Column(
              children: [
                const SizedBox(height: 4),
                _Hero(
                  size: heroSize,
                  animation: _heroAnimation,
                  transferFraction: _transferFraction,
                  searching: _peers.isEmpty && !_starting,
                  onTap: _sending ? null : _chooseAndSend,
                ),
                const SizedBox(height: 4),
                AnimatedSwitcher(
                  duration: const Duration(milliseconds: 220),
                  child: Text(
                    _error ?? _status,
                    key: ValueKey<String>(_error ?? _status),
                    textAlign: TextAlign.center,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      color: _transferFraction >= 0 ? _teal : (_error == null ? _muted : Colors.redAccent),
                      fontSize: 13,
                    ),
                  ),
                ),
                SizedBox(
                  height: 36,
                  child: _sending
                      ? TextButton.icon(
                          onPressed: _cancelSending,
                          icon: const Icon(Icons.close_rounded, size: 16),
                          label: const Text('取消发送', style: TextStyle(fontSize: 12)),
                        )
                      : Text(
                          _starting
                              ? '正在启动共享传输核心'
                              : selected != null
                                  ? '轻点墨洞选择文件'
                                  : _peers.isNotEmpty
                                      ? '点选下方设备作为目标'
                                      : '等待附近的墨洞上线',
                          style: const TextStyle(color: _dim, fontSize: 11),
                        ),
                ),
                if (_peers.isNotEmpty) ...[
                  SizedBox(
                    height: 54,
                    child: ListView.separated(
                      scrollDirection: Axis.horizontal,
                      itemCount: _peers.length,
                      separatorBuilder: (_, __) => const SizedBox(width: 8),
                      itemBuilder: (context, index) {
                        final peer = _peers[index];
                        return _DeviceChip(
                          peer: peer,
                          selected: peer.instanceId == _selectedInstance,
                          onTap: () => setState(() => _selectedInstance = peer.instanceId),
                        );
                      },
                    ),
                  ),
                  const SizedBox(height: 12),
                ],
                Row(
                  children: [
                    const Text(
                      '已接收',
                      style: TextStyle(color: _muted, fontSize: 13, fontWeight: FontWeight.w600),
                    ),
                    const Spacer(),
                    if (_received.isNotEmpty)
                      IconButton(
                        tooltip: '清空记录',
                        onPressed: _clearHistory,
                        icon: const Icon(Icons.delete_sweep_outlined, color: _dim, size: 19),
                      ),
                    TextButton.icon(
                      onPressed: _openInbox,
                      icon: const Icon(Icons.folder_outlined, color: _teal, size: 16),
                      label: const Text('收件箱', style: TextStyle(color: _teal, fontSize: 12)),
                    ),
                  ],
                ),
                if (_received.isEmpty)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 20),
                    child: Text('还没有收到过文件', style: TextStyle(color: _dim, fontSize: 12)),
                  )
                else
                  Expanded(
                    child: ListView.separated(
                      padding: const EdgeInsets.only(bottom: 16),
                      itemCount: _received.length,
                      separatorBuilder: (_, __) => const SizedBox(height: 6),
                      itemBuilder: (context, index) => _FileCard(file: _received[index]),
                    ),
                  ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _Hero extends StatelessWidget {
  const _Hero({
    required this.size,
    required this.animation,
    required this.transferFraction,
    required this.searching,
    required this.onTap,
  });

  final double size;
  final Animation<double> animation;
  final double transferFraction;
  final bool searching;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: '选择文件发送',
      child: GestureDetector(
        onTap: onTap,
        child: AnimatedBuilder(
          animation: animation,
          builder: (context, _) => CustomPaint(
            size: Size.square(size),
            painter: _HeroPainter(
              phase: animation.value,
              transferFraction: transferFraction,
              searching: searching,
            ),
          ),
        ),
      ),
    );
  }
}

class _HeroPainter extends CustomPainter {
  const _HeroPainter({
    required this.phase,
    required this.transferFraction,
    required this.searching,
  });

  final double phase;
  final double transferFraction;
  final bool searching;

  @override
  void paint(Canvas canvas, Size size) {
    final center = size.center(Offset.zero);
    final radius = size.shortestSide * .38;
    final breathe = .86 + sin(phase * pi * 2) * .08;
    final core = Paint()
      ..shader = RadialGradient(
        colors: const [Color(0xff15242a), Color(0xff05080b)],
      ).createShader(Rect.fromCircle(center: center, radius: radius * 1.4));
    canvas.drawCircle(center, radius * 1.2, core);

    final halo = Paint()
      ..color = _teal.withValues(alpha: .08 * breathe)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 9;
    canvas.drawCircle(center, radius * 1.06, halo);

    final arc = Paint()
      ..color = _teal.withValues(alpha: searching ? .32 : .62)
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeWidth = 4;
    final rect = Rect.fromCircle(center: center, radius: radius * .98);
    canvas.drawArc(rect, phase * pi * 2, pi * .72, false, arc);
    final second = Paint()
      ..color = _teal.withValues(alpha: .18)
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeWidth = 2;
    canvas.drawArc(rect, -phase * pi * 2 + pi, pi * .44, false, second);

    if (transferFraction >= 0) {
      final progress = Paint()
        ..color = _teal
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round
        ..strokeWidth = 5;
      canvas.drawArc(rect.inflate(5), -pi / 2, pi * 2 * transferFraction, false, progress);
    }

    final label = TextPainter(
      text: TextSpan(
        text: transferFraction >= 0 ? '${(transferFraction * 100).round()}%' : '发送',
        style: TextStyle(
          color: transferFraction >= 0 ? _teal : _text,
          fontSize: radius * .24,
          fontWeight: FontWeight.w700,
          letterSpacing: .4,
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    label.paint(canvas, center - Offset(label.width / 2, label.height / 2));
  }

  @override
  bool shouldRepaint(covariant _HeroPainter oldDelegate) =>
      oldDelegate.phase != phase ||
      oldDelegate.transferFraction != transferFraction ||
      oldDelegate.searching != searching;
}

class _DeviceChip extends StatelessWidget {
  const _DeviceChip({required this.peer, required this.selected, required this.onTap});

  final PeerView peer;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: selected ? _teal.withValues(alpha: .16) : _surface,
      borderRadius: BorderRadius.circular(8),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(8),
        child: Container(
          constraints: const BoxConstraints(minWidth: 116, maxWidth: 180),
          padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: selected ? _teal : _surfaceRaised),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(peer.viaSsh ? Icons.cloud_outlined : Icons.devices_outlined,
                  color: selected ? _teal : _muted, size: 18),
              const SizedBox(width: 7),
              Flexible(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(peer.name, maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: _text, fontSize: 12)),
                    Text(peer.shortId, style: const TextStyle(color: _dim, fontSize: 10)),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _FileCard extends StatelessWidget {
  const _FileCard({required this.file});

  final ReceivedFile file;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: _surface,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: _surfaceRaised),
      ),
      child: Row(
        children: [
          const Icon(Icons.insert_drive_file_outlined, color: _tealDim, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(file.name, maxLines: 1, overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: _text, fontSize: 12)),
                const SizedBox(height: 3),
                Text(_formatBytes(file.size), style: const TextStyle(color: _dim, fontSize: 10)),
              ],
            ),
          ),
          const Icon(Icons.check_circle_outline, color: _teal, size: 17),
        ],
      ),
    );
  }
}

int _asInt(dynamic value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? 0;
}

String _formatBytes(int bytes) {
  if (bytes < 1024) return '$bytes B';
  if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  if (bytes < 1024 * 1024 * 1024) return '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
  return '${(bytes / 1024 / 1024 / 1024).toStringAsFixed(2)} GB';
}

extension<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
