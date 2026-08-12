import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/exporter.dart';
import 'core/inkhole_core.dart';
import 'core/scanner.dart';
import 'models.dart';
import 'theme.dart';
import 'widgets/cross_network_dialog.dart';
import 'widgets/device_chip.dart';
import 'widgets/file_card.dart';
import 'widgets/ink_hole.dart';
import 'widgets/settings_dialog.dart';
import 'widgets/usage_guide_dialog.dart';

/// 主界面，结构照搬旧版 Android 的 MainActivity + InkHoleUI#MainScreen：
/// 顶栏（墨洞 / InkHole + 刷新 / 跨网络 / 设置）→ 墨洞英雄区 → 状态文案 →
/// 操作提示或取消发送 → 设备横排 → 已接收列表。
class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  static const MethodChannel _shareChannel =
      MethodChannel('com.rexvane.inkhole/share');

  final FlutterSecureStorage _secureStorage = const FlutterSecureStorage();
  late final InkHoleCore _core;
  StreamSubscription<Map<String, dynamic>>? _events;

  SharedPreferences? _preferences;
  String? _sessionId;
  String? _identityPrivate;
  String _peerName = 'Android';
  String _instanceId = '';
  int _listenPort = 0;
  int _actualPort = 0;
  String _inbox = '';
  String _rendezvousUrl = '';
  String _transitRelay = '';
  String _sshHost = '';
  String _sshUser = '';
  String _sshFingerprint = '';
  int _sshPort = 22;
  bool _sshEnabled = false;
  String? _sshSessionId;
  String _status = '正在启动…';
  String? _error;
  String? _selectedInstance;
  String? _activeWormholeSession;
  bool _starting = true;
  bool _sending = false;
  bool _encryptionEnabled = false;
  List<ManualPeer> _manualPeers = const <ManualPeer>[];
  List<PeerView> _peers = const <PeerView>[];

  final ValueNotifier<bool> _joining = ValueNotifier<bool>(false);
  final ValueNotifier<String?> _joinError = ValueNotifier<String?>(null);
  final ValueNotifier<bool> _oneTimeConnected = ValueNotifier<bool>(false);
  final ValueNotifier<bool> _sshPaired = ValueNotifier<bool>(false);
  final List<ReceivedFile> _received = <ReceivedFile>[];
  String _exportTreeUri = '';
  String _exportTreeLabel = '';

  /// 收件落点的完整可读路径，只用于设置里展示。
  String _exportPath = '';
  final List<String> _sharedFiles = <String>[];
  final Map<String, TransferProgress> _progress = <String, TransferProgress>{};

  /// 仍在进行的发送，key 与 _progress 一致。接收方向没有 send_id，
  /// 分开记才不会在纯接收时冒出「取消发送」。
  final Set<String> _sendIds = <String>{};
  final Map<String, Map<String, dynamic>> _earlySent =
      <String, Map<String, dynamic>>{};
  bool _sendRequestInFlight = false;

  /// 用户主动取消的发送。核心随后仍会回一条失败的 lan.sent，
  /// 不记下来就会把「已取消发送」盖成「发送失败」。
  final Set<String> _cancelledSends = <String>{};

  // 传输速率的指数平滑采样，对应旧版 MainActivity#onProgress。
  String _speedKey = '';
  int _speedSampleTime = 0;
  int _speedSampleDone = 0;
  double _speedBytes = 0;

  @override
  void initState() {
    super.initState();
    _core = InkHoleCore();
    _shareChannel.setMethodCallHandler(_onShareMethodCall);
    unawaited(_loadSharedFiles());
    unawaited(_boot());
  }

  @override
  void dispose() {
    _events?.cancel();
    _joining.dispose();
    _joinError.dispose();
    _oneTimeConnected.dispose();
    _sshPaired.dispose();
    _shareChannel.setMethodCallHandler(null);
    unawaited(_stopCore());
    super.dispose();
  }

  // ---- 启动与配置 ----

  Future<void> _loadSharedFiles() async {
    try {
      final List<String> paths =
          await _shareChannel.invokeListMethod<String>('consumeSharedFiles') ??
              const <String>[];
      final List<String> errors =
          await _shareChannel.invokeListMethod<String>('consumeShareErrors') ??
              const <String>[];
      _queueSharedFiles(paths);
      if (errors.isNotEmpty) {
        _setStatus(
          errors.length == 1
              ? '分享文件未加入：${errors.first}'
              : '有 ${errors.length} 个分享文件未加入：${errors.first}',
        );
      }
    } catch (_) {
      // The channel is Android-only; other Flutter targets simply use the picker.
    }
  }

  Future<dynamic> _onShareMethodCall(MethodCall call) async {
    if (call.method == 'sharedFiles' && call.arguments is List) {
      _queueSharedFiles((call.arguments as List<dynamic>).whereType<String>());
    } else if (call.method == 'shareError' && call.arguments is String) {
      _setStatus(call.arguments as String);
    }
    return null;
  }

  void _queueSharedFiles(Iterable<String> paths) {
    final List<String> fresh = paths
        .map((String path) => path.trim())
        .where((String path) => path.isNotEmpty && !_sharedFiles.contains(path))
        .toList(growable: false);
    if (fresh.isEmpty || !mounted) return;
    setState(() => _sharedFiles.addAll(fresh));
    _setStatus('收到 ${_sharedFiles.length} 个待发送文件');
  }

  Future<void> _boot() async {
    try {
      _preferences = await SharedPreferences.getInstance();
      _peerName =
          _preferences!.getString('peer_name') ?? Platform.localHostname;
      if (_peerName.trim().isEmpty) _peerName = 'Android';
      _inbox = await _resolveInbox();
      _encryptionEnabled = _preferences!.getBool('encryption_enabled') ?? false;
      // The export contains the device TLS and signing private keys; keep it
      // in platform secure storage instead of ordinary app preferences.
      _identityPrivate = await _secureStorage.read(key: 'identity_private');
      final String? legacyIdentity =
          _preferences!.getString('identity_private');
      if (_identityPrivate == null || _identityPrivate!.trim().isEmpty) {
        if (legacyIdentity != null && legacyIdentity.trim().isNotEmpty) {
          await _secureStorage.write(
            key: 'identity_private',
            value: legacyIdentity,
          );
          _identityPrivate = legacyIdentity;
          await _preferences!.remove('identity_private');
        }
      } else if (legacyIdentity != null) {
        await _preferences!.remove('identity_private');
      }
      _loadStoredSettings();
      unawaited(_refreshExportPath());
      _events = _core.events.listen(_onCoreEvent);
      await _core.start();
      await _startLanSession();
      if (!mounted) return;
      setState(() {
        _starting = false;
        _status = '等待附近的墨洞上线…';
      });
      if (_sharedFiles.isNotEmpty) {
        _setStatus('收到 ${_sharedFiles.length} 个待发送文件');
      }
      await _maybeShowUsageGuide();
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _starting = false;
        _error = error.toString();
        _status = '共享传输核心暂不可用';
      });
    }
  }

  void _loadStoredSettings() {
    final SharedPreferences prefs = _preferences!;
    // 默认固定端口:便于防火墙放行与 Tailscale 手动直连;被占用时核心自动
    // 退回随机端口。用户在设置里改为空则回到随机(0)。
    _listenPort = prefs.getInt('listen_port') ?? 41300;
    _sshHost = prefs.getString('ssh_host') ?? '';
    _sshUser = prefs.getString('ssh_user') ?? '';
    _sshFingerprint = prefs.getString('ssh_fingerprint') ?? '';
    _sshPort = prefs.getInt('ssh_port') ?? 22;
    _sshEnabled = prefs.getBool('ssh_enabled') ?? false;
    _rendezvousUrl = prefs.getString('rendezvous_url') ?? '';
    _transitRelay = prefs.getString('transit_relay') ?? '';
    _exportTreeUri = prefs.getString('export_tree_uri') ?? '';
    _exportTreeLabel = prefs.getString('export_tree_label') ?? '';
    _manualPeers = (prefs.getStringList('manual_peers') ?? const <String>[])
        .map(ManualPeer.decode)
        .where((ManualPeer peer) => peer.host.isNotEmpty)
        .toList(growable: false);
  }

  Future<void> _startLanSession() async {
    _instanceId = _loadInstanceId();
    Map<String, dynamic> result;
    try {
      result = await _lanStartCall();
    } on Exception catch (error) {
      // 端口尚在释放中的极端竞态:核心已支持启动时自动清理旧会话,
      // 等一秒重试一次即可恢复。
      if ('$error'.contains('Address already in use')) {
        await Future<void>.delayed(const Duration(seconds: 1));
        result = await _lanStartCall();
      } else {
        rethrow;
      }
    }
    await _applyLanStartResult(result);
  }

  Future<Map<String, dynamic>> _lanStartCall() async {
    return _core.call('lan.start', <String, dynamic>{
      'peer_name': _peerName,
      'instance_id': _instanceId,
      'identity_private': _identityPrivate ?? '',
      'secret': _encryptionEnabled
          ? await _secureStorage.read(key: 'transfer_secret') ?? ''
          : '',
      'inbox': _inbox,
      'inbox_category_roots': <String, dynamic>{},
      'listen_port': _listenPort,
      'capabilities': const <String>['quic-v2', 'blake3', 'folder-v1'],
      'discovery_targets': _manualPeers.map((ManualPeer peer) {
        if (peer.port == 0) return peer.host;
        if (peer.host.contains(':') && !peer.host.startsWith('[')) {
          return '[${peer.host}]:${peer.port}';
        }
        return '${peer.host}:${peer.port}';
      }).toList(growable: false),
    });
  }

  Future<void> _applyLanStartResult(Map<String, dynamic> result) async {
    _sessionId = result['session_id']?.toString();
    _actualPort = asInt(result['port']);
    _identityPrivate = result['identity_private']?.toString();
    final String? identityPrivate = _identityPrivate;
    if (identityPrivate != null) {
      // 设备身份(TLS 证书 + ed25519 私钥)必须落盘后才能算启动成功;否则
      // 首启崩溃或安全存储写入失败会导致下次冷启生成全新身份,对端固定指纹
      // 失配(SSH 配对断裂、被当新设备)。await 而非 unawaited,失败时提示。
      try {
        await _secureStorage.write(
            key: 'identity_private', value: identityPrivate);
      } catch (_) {
        _setStatus('身份保存失败,重启后可能需要重新配对');
      }
    }
    if (_sshEnabled) unawaited(_startSsh());
  }

  /// 改了设备名/口令/手动设备后重建局域网会话，对应旧版保存设置后重启前台服务。
  Future<void> _restartLan() async {
    final String? previous = _sessionId;
    _sessionId = null;
    _sshSessionId = null;
    if (previous != null) {
      try {
        await _core.call('lan.stop', <String, dynamic>{'session_id': previous});
      } catch (_) {}
    }
    if (mounted) {
      setState(() {
        _peers = const <PeerView>[];
        _selectedInstance = null;
        _progress.clear();
        _sendIds.clear();
        _earlySent.clear();
        _sendRequestInFlight = false;
        _cancelledSends.clear();
        _sending = false;
      });
    }
    try {
      await _startLanSession();
      _setStatus('设置已生效，正在重新搜索设备…');
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error.toString();
        _status = '重启共享传输核心失败';
      });
    }
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
      'private_key':
          privateKey ?? await _secureStorage.read(key: 'ssh_private_key') ?? '',
      'private_key_label': 'Flutter secure storage',
      'passphrase':
          passphrase ?? await _secureStorage.read(key: 'ssh_passphrase') ?? '',
      'host_key_sha256': _sshFingerprint,
    };
  }

  Future<void> _startSsh() async {
    if (_sessionId == null || !_sshEnabled) return;
    final Map<String, dynamic> profile = await _sshProfile();
    if (profile['host'].toString().trim().isEmpty ||
        profile['user'].toString().trim().isEmpty ||
        profile['private_key'].toString().trim().isEmpty ||
        profile['host_key_sha256'].toString().trim().isEmpty) {
      _setStatus('SSH 设置不完整，请先验证主机指纹');
      return;
    }
    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.listen', <String, dynamic>{
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
    final Directory root = await getApplicationDocumentsDirectory();
    final Directory inbox = Directory(p.join(root.path, 'InkHole'));
    await inbox.create(recursive: true);
    return inbox.path;
  }

  String _loadInstanceId() {
    final String? existing = _preferences!.getString('instance_id');
    if (existing != null && RegExp(r'^[a-f0-9]{32}$').hasMatch(existing)) {
      return existing;
    }
    final Random random = Random.secure();
    final String id = List<String>.generate(
      32,
      (int index) => random.nextInt(16).toRadixString(16),
    ).join();
    unawaited(_preferences!.setString('instance_id', id));
    return id;
  }

  Future<void> _stopCore() async {
    if (_sshSessionId != null) {
      try {
        await _core.call(
          'session.cancel',
          <String, dynamic>{'session_id': _sshSessionId},
        );
      } catch (_) {}
    }
    if (_sessionId != null) {
      try {
        await _core.call('lan.stop', <String, dynamic>{
          'session_id': _sessionId,
        });
      } catch (_) {}
    }
    await _core.dispose();
  }

  // ---- 核心事件 ----

  void _onCoreEvent(Map<String, dynamic> event) {
    final String name = event['event']?.toString() ?? '';
    final Map<String, dynamic> data = Map<String, dynamic>.from(
        event['data'] as Map? ?? const <String, dynamic>{});
    if (name == 'lan.peers' ||
        name == 'lan.status' ||
        name == 'lan.progress' ||
        name == 'lan.sent' ||
        name == 'lan.received') {
      final String? eventSession = data['session_id']?.toString();
      if (eventSession != null && eventSession != _sessionId) return;
    }
    switch (name) {
      case 'core.fatal':
        _joining.value = false;
        _oneTimeConnected.value = false;
        if (!mounted) return;
        setState(() {
          _error = data['message']?.toString() ?? '共享传输核心已停止';
          _status = '共享传输核心已停止';
          _sending = false;
          _progress.clear();
          _sendIds.clear();
          _earlySent.clear();
          _sendRequestInFlight = false;
          _cancelledSends.clear();
        });
      case 'lan.peers':
        // 旧会话残余的 peers 快照会污染设备列表(尤其 _restartLan 期间),
        // 只处理当前会话事件;_sessionId 为 null(重启中)时也忽略。
        final String? peersSession = data['session_id']?.toString();
        if (peersSession != null && peersSession != _sessionId) return;
        final List<dynamic> values =
            data['peers'] as List<dynamic>? ?? const <dynamic>[];
        if (!mounted) return;
        setState(() {
          _peers = values
              .whereType<Map<dynamic, dynamic>>()
              .map((Map<dynamic, dynamic> value) =>
                  PeerView.fromJson(Map<String, dynamic>.from(value)))
              .toList(growable: false);
          if (_selectedInstance != null &&
              !_peers.any(
                  (PeerView peer) => peer.instanceId == _selectedInstance)) {
            _selectedInstance = null;
          }
          if (_peers.isNotEmpty && _status == '等待附近的墨洞上线…') {
            _status = '发现 ${_peers.length} 台设备';
          }
        });
      case 'lan.status':
        // 与 lan.peers 同理:重启期间(_sessionId 为 null)忽略旧会话状态,
        // 避免旧状态文字短暂覆盖"设置已生效"等新提示。
        final String? statusSession = data['session_id']?.toString();
        if (statusSession != null && statusSession != _sessionId) return;
        _setStatus(data['message']?.toString() ?? '局域网状态已更新');
      case 'lan.progress':
        final String key = data['send_id']?.toString() ??
            data['transfer_id']?.toString() ??
            name;
        final String filename = data['filename']?.toString() ?? '文件';
        final int done = asInt(data['done']);
        final int total = asInt(data['total']);
        final String line = _progressLine(
          data['kind']?.toString() ?? 'send',
          filename,
          done,
          total,
        );
        if (!mounted) return;
        setState(() {
          _progress[key] = TransferProgress(filename, done, total);
          _status = line;
        });
      case 'lan.sent':
        final String? id = data['send_id']?.toString();
        if (id != null && !_sendIds.contains(id)) {
          if (_sendRequestInFlight) {
            _earlySent[id] = Map<String, dynamic>.from(data);
          }
          return;
        }
        final bool cancelled = id != null && _cancelledSends.remove(id);
        if (!mounted) return;
        setState(() {
          if (id != null) {
            _progress.remove(id);
            _sendIds.remove(id);
          }
          _sending = _sendIds.isNotEmpty;
        });
        if (cancelled) {
          _setStatus('已取消发送');
        } else {
          _setStatus(
            data['ok'] == true ? '发送完成' : '发送失败：${data['error'] ?? '连接中断'}',
          );
        }
      case 'lan.received':
        final String path = data['path']?.toString() ?? '';
        final String? receiveId =
            data['send_id']?.toString() ?? data['transfer_id']?.toString();
        if (!mounted) return;
        setState(() {
          // 收完就把进度条撤掉：后面还要把成品搬到下载目录，
          // 那段时间进度环停在 100% 会被当成卡住。
          if (receiveId != null) _progress.remove(receiveId);
          _sending = _sendIds.isNotEmpty;
          _received.insert(
            0,
            ReceivedFile(
              name: data['filename']?.toString() ?? p.basename(path),
              path: path,
              size: asInt(data['size']),
              receivedAt: DateTime.now(),
              sender: data['sender_name']?.toString() ?? '',
            ),
          );
        });
        if (path.isEmpty) {
          _setStatus('已接收文件');
        } else {
          unawaited(_exportReceived(path));
        }
      case 'wormhole.offer':
        _activeWormholeSession = data['session_id']?.toString();
        if (mounted) unawaited(_showWormholeOffer(data));
      case 'wormhole.ready':
        _joining.value = false;
        _joinError.value = null;
        _oneTimeConnected.value = true;
        _setStatus('跨网络传输已连接');
      case 'wormhole.error':
        _joining.value = false;
        _oneTimeConnected.value = false;
        _activeWormholeSession = null;
        final String reason = data['error']?.toString() ?? '连接中断';
        _joinError.value = reason;
        _setStatus('短码传输失败：$reason');
      case 'ssh.paired':
        _sshPaired.value = true;
        _toast('SSH 设备已配对');
      case 'ssh.ready':
        _setStatus(data['connected'] == true ? 'SSH 中继已连接' : 'SSH 中继正在重连');
      case 'ssh.connected':
        _setStatus('SSH 中继已恢复');
      case 'ssh.disconnected':
        _setStatus('SSH 中继已断开，正在重连');
      case 'ssh.channel.error':
      case 'ssh.reconnect.error':
      case 'ssh.data.error':
      case 'ssh.config.error':
      case 'ssh.pair.error':
        _setStatus(data['error']?.toString() ?? 'SSH 中继异常');
    }
  }

  /// 百分比与速度放最前：状态栏空间不够时截断的是文件名，实时速度永远可见。
  ///
  /// 满 100% 之后核心还要 fsync + 落盘校验，这段时间不会再有事件，
  /// 所以文字换成「正在写入存储…」，免得看起来像卡死。
  String _progressLine(String kind, String filename, int done, int total) {
    final String key = '$kind|$filename';
    final int now = DateTime.now().millisecondsSinceEpoch;
    if (_speedKey != key || done < _speedSampleDone) {
      _speedKey = key;
      _speedSampleTime = now;
      _speedSampleDone = done;
      _speedBytes = 0;
    } else {
      final int elapsed = now - _speedSampleTime;
      if (elapsed > 0) {
        final double instant = (done - _speedSampleDone) * 1000 / elapsed;
        if (_speedBytes <= 0) {
          _speedBytes = instant;
        } else {
          _speedBytes = _speedBytes * 0.65 + instant * 0.35;
        }
        _speedSampleTime = now;
        _speedSampleDone = done;
      }
    }
    final int percent = total > 0 ? (done * 100 / total).round() : 100;
    final String direction = kind == 'send' ? '↑ 发送' : '↓ 接收';
    if (total > 0 && done >= total) {
      final String tail = kind == 'send' ? '正在校验…' : '正在写入存储…';
      return '$direction 100% · $tail · $filename';
    }
    String speed = '';
    if (_speedBytes >= 1024 * 1024) {
      speed = ' · ${(_speedBytes / 1024 / 1024).toStringAsFixed(1)} MB/s';
    } else if (_speedBytes >= 1024) {
      speed = ' · ${(_speedBytes / 1024).toStringAsFixed(0)} KB/s';
    }
    return '$direction $percent%$speed · $filename';
  }

  // ---- 发送 ----

  Future<void> _refresh() async {
    if (_sessionId == null) return;
    try {
      await _core.call('lan.refresh', <String, dynamic>{
        'session_id': _sessionId,
      });
      _setStatus('正在重新搜索设备…');
    } catch (error) {
      _setStatus('刷新失败：$error');
    }
  }

  void _onDeviceTap(PeerView peer) {
    final String? next =
        _selectedInstance == peer.instanceId ? null : peer.instanceId;
    setState(() => _selectedInstance = next);
    if (next == null || _sharedFiles.isEmpty) return;
    if (_sendRequestInFlight || _sending) return;
    final List<String> queued = _sharedFiles.toList(growable: false);
    setState(() => _sharedFiles.clear());
    unawaited(_sendPaths(queued, peer));
  }

  Future<void> _chooseAndSend() async {
    if (_sendRequestInFlight || _sending) return;
    if (_sessionId == null) {
      _setStatus('墨洞未就绪');
      return;
    }
    final PeerView? target = _selectedPeer;
    if (target == null) {
      _setStatus(_peers.isEmpty ? '还没发现设备' : '先点选一台目标设备');
      return;
    }
    if (_sharedFiles.isNotEmpty) {
      final List<String> queued = _sharedFiles.toList(growable: false);
      setState(() => _sharedFiles.clear());
      await _sendPaths(queued, target);
      return;
    }
    final List<String> paths = await _pickPaths();
    if (paths.isEmpty) return;
    await _sendPaths(paths, target);
  }

  Future<void> _sendPaths(List<String> paths, PeerView target) async {
    if (_sendRequestInFlight || _sending) return;
    _sendRequestInFlight = true;
    if (mounted) setState(() => _sending = true);
    for (final String path in paths) {
      if (_sessionId == null) {
        _sendRequestInFlight = false;
        if (mounted && _sendIds.isEmpty) setState(() => _sending = false);
        return;
      }
      try {
        final Map<String, dynamic> response =
            await _core.call('lan.send', <String, dynamic>{
          'session_id': _sessionId,
          'path': path,
          'instance_id': target.instanceId,
          'host': target.host,
          'port': target.port,
          'fingerprint': target.fingerprint,
          'endpoint_token': '',
        });
        final String? id = response['send_id']?.toString();
        if (id != null && mounted) {
          setState(() {
            _sendIds.add(id);
            _sending = true;
          });
          final Map<String, dynamic>? early = _earlySent.remove(id);
          if (early != null) {
            _onCoreEvent(<String, dynamic>{
              'event': 'lan.sent',
              'data': early,
            });
          }
        }
      } catch (error) {
        _setStatus('发送失败：$error');
      }
    }
    _sendRequestInFlight = false;
    if (mounted && _sendIds.isEmpty) setState(() => _sending = false);
  }

  /// 中断所有在途发送。核心只提供 lan.send.cancel，接收方向没有
  /// 单条取消的接口（取消只能整体停会话），所以这里只管发送。
  Future<void> _cancelSending() async {
    if (_sessionId == null) return;
    final List<String> sends = _sendIds.toList(growable: false);
    if (sends.isEmpty) return;
    _cancelledSends.addAll(sends);
    for (final String sendId in sends) {
      try {
        await _core.call('lan.send.cancel', <String, dynamic>{
          'session_id': _sessionId,
          'send_id': sendId,
        });
      } catch (_) {}
    }
    if (mounted) {
      setState(() {
        for (final String sendId in sends) {
          _progress.remove(sendId);
          _sendIds.remove(sendId);
        }
        _sending = _sendIds.isNotEmpty;
      });
    }
    _setStatus('已取消发送');
  }

  Future<List<String>> _pickPaths() async {
    final FilePickerResult? result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: false,
    );
    return result?.files
            .map((PlatformFile file) => file.path)
            .whereType<String>()
            .where((String path) => path.isNotEmpty)
            .toList(growable: false) ??
        const <String>[];
  }

  // ---- 跨网络 ----

  Map<String, dynamic> _wormholeSettings() => <String, dynamic>{
        'rendezvous_url': _rendezvousUrl,
        'transit_relay': _transitRelay,
        'timeout_minutes': 10,
      };

  Future<void> _createOneTime() async {
    final List<String> paths = await _pickPaths();
    if (paths.isEmpty || _sessionId == null) return;
    _oneTimeConnected.value = false;
    try {
      final Map<String, dynamic> result =
          await _core.call('wormhole.create', <String, dynamic>{
        'session_id': _sessionId,
        'paths': paths,
        'settings': _wormholeSettings(),
      });
      _activeWormholeSession = result['session_id']?.toString();
      if (!mounted) return;
      await _showShortCode(
        result,
        title: '一次性发送短码',
        connected: _oneTimeConnected,
      );
    } catch (error) {
      _setStatus('无法生成短码：$error');
    }
  }

  /// 返回 true 表示已经连上，面板会自己关掉；返回 false 时保留面板等用户重试。
  Future<bool> _joinOneTime(String code) async {
    if (_sessionId == null || code.trim().isEmpty) return false;
    _joinError.value = null;
    _joining.value = true;
    try {
      final Map<String, dynamic> result =
          await _core.call('wormhole.join.start', <String, dynamic>{
        'session_id': _sessionId,
        'code': code.trim(),
        'settings': _wormholeSettings(),
      });
      _activeWormholeSession = result['session_id']?.toString();
      _setStatus('等待发送端确认内容');
      return true;
    } catch (error) {
      _joining.value = false;
      _joinError.value = '短码无效：$error';
      _setStatus('短码无效：$error');
      return false;
    }
  }

  Future<void> _showCrossNetwork() async {
    _joinError.value = null;
    await showDialog<void>(
      context: context,
      builder: (BuildContext dialogContext) => CrossNetworkActionsDialog(
        joining: _joining,
        joinError: _joinError,
        sshReady: _sshSessionId != null,
        initialReceiveCode: '',
        onOneTimeSend: () => unawaited(_createOneTime()),
        onScan: _scanShortCode,
        onJoinOneTime: _joinOneTime,
        onCreateSshPair: () => unawaited(_createSshPair()),
        onJoinSshPair: (String code) => unawaited(_joinSshPair(code)),
      ),
    );
  }

  /// 扫一次性短码二维码；取消返回 null，扫到无关内容时提示并返回 null。
  Future<String?> _scanShortCode() async {
    final String? raw;
    try {
      raw = await ScannerChannel.scan();
    } on Exception catch (error) {
      _toast(friendlyError(error));
      return null;
    }
    if (raw == null) return null;
    final String? code = parseScannedCode(raw);
    if (code == null) {
      _toast('二维码不是有效的一次性短码');
      return null;
    }
    return code;
  }

  Future<void> _createSshPair() async {
    final String? session = _sshSessionId;
    if (session == null) {
      _setStatus('SSH 中继未连接');
      return;
    }
    _sshPaired.value = false;
    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.pair.create', <String, dynamic>{
        'session_id': session,
      });
      if (!mounted) return;
      await _showShortCode(result, title: 'SSH 设备配对', connected: _sshPaired);
    } catch (error) {
      _setStatus('SSH 配对不可用：$error');
    }
  }

  Future<void> _joinSshPair(String code) async {
    final String? session = _sshSessionId;
    if (session == null || code.trim().isEmpty) return;
    try {
      await _core.call('ssh.pair.join', <String, dynamic>{
        'session_id': session,
        'code': code.trim(),
      });
      _setStatus('SSH 设备已加入');
    } catch (error) {
      _setStatus('SSH 配对失败：$error');
    }
  }

  Future<void> _showShortCode(
    Map<String, dynamic> result, {
    required String title,
    required ValueNotifier<bool> connected,
  }) async {
    final String code = result['code']?.toString() ?? '';
    await showDialog<void>(
      context: context,
      builder: (BuildContext dialogContext) => ShortCodeDisplayDialog(
        title: title,
        code: code,
        uri: result['uri']?.toString() ?? '',
        connected: connected,
        onCopy: () => _copyText(code, '短码已复制'),
      ),
    );
  }

  Future<void> _showWormholeOffer(Map<String, dynamic> data) async {
    if (!mounted) return;
    final String? session =
        data['session_id']?.toString() ?? _activeWormholeSession;
    final bool? accept = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (BuildContext dialogContext) => WormholeOfferDialog(
        summary: Map<String, dynamic>.from(
            data['summary'] as Map? ?? const <String, dynamic>{}),
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

  // ---- 设置 ----

  Future<void> _openSettings() async {
    final SharedPreferences? prefs = _preferences;
    if (prefs == null) {
      _setStatus('配置尚未就绪，请稍候再试');
      return;
    }
    final String secret =
        await _secureStorage.read(key: 'transfer_secret') ?? '';
    final String sshKey =
        await _secureStorage.read(key: 'ssh_private_key') ?? '';
    final String sshPassphrase =
        await _secureStorage.read(key: 'ssh_passphrase') ?? '';
    if (!mounted) return;
    final String portLine = _actualPort > 0
        ? '端口：$_actualPort（建议自定义 1024-49151 固定端口）'
        : '端口：未启动（建议自定义 1024-49151 固定端口）';
    final InkSettings? saved = await showDialog<InkSettings>(
      context: context,
      builder: (BuildContext dialogContext) => SettingsDialog(
        initial: InkSettings(
          peerName: _peerName,
          listenPort: _listenPort,
          encryptionEnabled: _encryptionEnabled,
          secret: secret,
          manualPeers: _manualPeers,
          rendezvousUrl: _rendezvousUrl,
          transitRelay: _transitRelay,
          sshEnabled: _sshEnabled,
          sshHost: _sshHost,
          sshPort: _sshPort,
          sshUser: _sshUser,
          sshFingerprint: _sshFingerprint,
          sshPrivateKey: sshKey,
          sshPassphrase: sshPassphrase,
        ),
        deviceLine: '本机：$_peerName-${_shortInstanceId()}',
        portLine: portLine,
        inboxPath: _inbox,
        exportPath: _exportPath.isEmpty ? '系统下载目录/InkHole' : _exportPath,
        onPickExportDirectory: () async {
          final PickedDirectory? picked = await ExporterChannel.pickDirectory();
          if (picked == null) return null;
          await _saveExportDirectory(picked.uri, picked.label);
          return _exportPath;
        },
        onResetExportDirectory: () async {
          await _saveExportDirectory('', '');
          return _exportPath;
        },
        sshReady: _sshSessionId != null,
        onCheckSsh: _checkSsh,
        onCreateSshPair: () => unawaited(_createSshPair()),
        onOpenCrossNetwork: () => unawaited(_showCrossNetwork()),
        onOpenRepository: () => _copyText(repositoryUrl, '仓库地址已复制'),
      ),
    );
    if (saved == null) return;
    await _applySettings(saved);
  }

  String _shortInstanceId() =>
      _instanceId.length > 8 ? _instanceId.substring(0, 8) : _instanceId;

  Future<Map<String, dynamic>?> _checkSsh(InkSettings draft) async {
    if (draft.sshHost.isEmpty ||
        draft.sshUser.isEmpty ||
        draft.sshPrivateKey.isEmpty) {
      _toast('请完整填写 SSH 主机、用户和私钥');
      return null;
    }
    try {
      return await _core.call('ssh.check', <String, dynamic>{
        'profile': <String, dynamic>{
          'id': 'mobile-default',
          'host': draft.sshHost,
          'port': draft.sshPort,
          'user': draft.sshUser,
          'private_key': draft.sshPrivateKey,
          'private_key_label': 'Flutter secure storage',
          'passphrase': draft.sshPassphrase,
          'host_key_sha256': draft.sshFingerprint,
        },
      });
    } catch (error) {
      _toast('SSH 检查失败：$error');
      return null;
    }
  }

  Future<void> _applySettings(InkSettings settings) async {
    final SharedPreferences? prefs = _preferences;
    if (prefs == null) return;
    final String previousSecret =
        await _secureStorage.read(key: 'transfer_secret') ?? '';
    final String previousSshKey =
        await _secureStorage.read(key: 'ssh_private_key') ?? '';
    final String previousSshPassphrase =
        await _secureStorage.read(key: 'ssh_passphrase') ?? '';
    final bool restartNeeded = settings.peerName != _peerName ||
        settings.listenPort != _listenPort ||
        settings.encryptionEnabled != _encryptionEnabled ||
        settings.secret != previousSecret ||
        !_sameManualPeers(settings.manualPeers, _manualPeers);
    final bool sshRestartNeeded = settings.sshEnabled != _sshEnabled ||
        settings.sshHost != _sshHost ||
        settings.sshPort != _sshPort ||
        settings.sshUser != _sshUser ||
        settings.sshFingerprint != _sshFingerprint ||
        settings.sshPrivateKey != previousSshKey ||
        settings.sshPassphrase != previousSshPassphrase;

    await prefs.setString('peer_name', settings.peerName);
    await prefs.setInt('listen_port', settings.listenPort);
    await prefs.setBool('encryption_enabled', settings.encryptionEnabled);
    await prefs.setStringList(
      'manual_peers',
      settings.manualPeers
          .map((ManualPeer peer) => peer.encode())
          .toList(growable: false),
    );
    await prefs.setString('rendezvous_url', settings.rendezvousUrl);
    await prefs.setString('transit_relay', settings.transitRelay);
    await prefs.setBool('ssh_enabled', settings.sshEnabled);
    await prefs.setString('ssh_host', settings.sshHost);
    await prefs.setInt('ssh_port', _validPort(settings.sshPort));
    await prefs.setString('ssh_user', settings.sshUser);
    await prefs.setString('ssh_fingerprint', settings.sshFingerprint);
    if (settings.encryptionEnabled && settings.secret.isNotEmpty) {
      await _secureStorage.write(
        key: 'transfer_secret',
        value: settings.secret,
      );
    } else if (!settings.encryptionEnabled) {
      await _secureStorage.delete(key: 'transfer_secret');
    }
    if (settings.sshPrivateKey.isNotEmpty) {
      await _secureStorage.write(
        key: 'ssh_private_key',
        value: settings.sshPrivateKey,
      );
    } else {
      await _secureStorage.delete(key: 'ssh_private_key');
    }
    if (settings.sshPassphrase.isNotEmpty) {
      await _secureStorage.write(
        key: 'ssh_passphrase',
        value: settings.sshPassphrase,
      );
    } else {
      await _secureStorage.delete(key: 'ssh_passphrase');
    }
    final bool sshWasEnabled = _sshEnabled;
    if (!mounted) return;
    setState(() {
      _peerName = settings.peerName;
      _listenPort = settings.listenPort;
      _encryptionEnabled = settings.encryptionEnabled;
      _manualPeers = settings.manualPeers;
      _rendezvousUrl = settings.rendezvousUrl;
      _transitRelay = settings.transitRelay;
      _sshEnabled = settings.sshEnabled;
      _sshHost = settings.sshHost;
      _sshPort = _validPort(settings.sshPort);
      _sshUser = settings.sshUser;
      _sshFingerprint = settings.sshFingerprint;
    });
    if (!settings.sshEnabled && _sshSessionId != null) {
      try {
        await _core.call(
          'session.cancel',
          <String, dynamic>{'session_id': _sshSessionId},
        );
      } catch (_) {}
      _sshSessionId = null;
    } else if (settings.sshEnabled && sshRestartNeeded && !restartNeeded) {
      if (_sshSessionId != null) {
        try {
          await _core.call(
            'session.cancel',
            <String, dynamic>{'session_id': _sshSessionId},
          );
        } catch (_) {}
      }
      _sshSessionId = null;
      unawaited(_startSsh());
    }
    if (restartNeeded) {
      await _restartLan();
      return;
    }
    if (settings.sshEnabled &&
        !sshRestartNeeded &&
        (!sshWasEnabled || _sshSessionId == null)) {
      unawaited(_startSsh());
    }
    _setStatus('设置已保存');
  }

  bool _sameManualPeers(List<ManualPeer> left, List<ManualPeer> right) {
    if (left.length != right.length) return false;
    for (int index = 0; index < left.length; index++) {
      if (left[index].name != right[index].name ||
          left[index].host != right[index].host ||
          left[index].port != right[index].port) {
        return false;
      }
    }
    return true;
  }

  int _validPort(int value) {
    if (value < 1) return 1;
    if (value > 65535) return 65535;
    return value;
  }

  Future<void> _maybeShowUsageGuide() async {
    final SharedPreferences? prefs = _preferences;
    if (prefs == null) return;
    if (prefs.getBool('usage_guide_seen') ?? false) return;
    await prefs.setBool('usage_guide_seen', true);
    if (!mounted) return;
    await showUsageGuide(context);
  }

  // ---- 杂项 ----

  /// 收件成品导出到公共位置(默认 Download/InkHole,或用户自定义目录)。
  ///
  /// GB 级文件搬运要几十秒，期间进度条已经撤掉了，得用状态文字说明还在忙。
  Future<void> _exportReceived(String path) async {
    final String target = _exportTreeLabel.isEmpty ? '下载目录' : _exportTreeLabel;
    _setStatus('正在保存到$target…');
    try {
      final ExportOutcome outcome = await ExporterChannel.export(
        path,
        treeUri: _exportTreeUri.isEmpty ? null : _exportTreeUri,
      );
      if (!mounted) return;
      if (outcome.location.isEmpty) {
        _setStatus('已接收，文件留在应用内收件箱');
        return;
      }
      final int index =
          _received.indexWhere((ReceivedFile file) => file.path == path);
      if (index >= 0) {
        setState(() {
          _received[index] = ReceivedFile(
            name: outcome.name.isEmpty ? _received[index].name : outcome.name,
            path: '${outcome.location}/${outcome.name}',
            size: _received[index].size,
            receivedAt: _received[index].receivedAt,
            sender: _received[index].sender,
          );
        });
      }
      _setStatus('已保存到 ${outcome.location}');
    } on Exception {
      // 导出失败留在应用内目录,记录保持原路径即可。
      _setStatus('已接收，文件留在应用内收件箱');
    }
  }

  /// 轻点收件记录直接交给系统打开；文件夹或已被移走的条目回退到下载管理。
  Future<void> _openReceived(ReceivedFile file) async {
    try {
      final String outcome = await ExporterChannel.open(
        path: file.path,
        name: file.name,
        treeUri: _exportTreeUri,
      );
      if (outcome == ExporterChannel.openedDownloads) {
        _toast('已打开系统下载目录，请在 InkHole 文件夹里查看');
      }
    } on Exception catch (error) {
      _toast(friendlyError(error));
    }
  }

  /// 设置弹窗里选择/恢复收件目录后的持久化。
  Future<void> _saveExportDirectory(String uri, String label) async {
    _exportTreeUri = uri;
    _exportTreeLabel = label;
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    if (uri.isEmpty) {
      await prefs.remove('export_tree_uri');
      await prefs.remove('export_tree_label');
    } else {
      await prefs.setString('export_tree_uri', uri);
      await prefs.setString('export_tree_label', label);
    }
    await _refreshExportPath();
  }

  /// 收件落点的完整路径：默认目录问原生要绝对路径，自定义目录把树 URI 解开。
  Future<String> _refreshExportPath() async {
    String resolved;
    if (_exportTreeUri.isEmpty) {
      resolved = await ExporterChannel.downloadsPath() ?? '';
      if (resolved.isEmpty) resolved = '系统下载目录/InkHole';
    } else {
      resolved = await ExporterChannel.describeTree(_exportTreeUri) ?? '';
      if (resolved.isEmpty) {
        resolved = _exportTreeLabel.isEmpty ? '自定义目录' : _exportTreeLabel;
      }
    }
    if (mounted) setState(() => _exportPath = resolved);
    return resolved;
  }

  void _setStatus(String value) {
    if (mounted) setState(() => _status = value);
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        duration: const Duration(seconds: 3),
        backgroundColor: inkBgCardActive,
      ),
    );
  }

  void _copyText(String value, String message) {
    unawaited(Clipboard.setData(ClipboardData(text: value)));
    _toast(message);
  }

  Future<void> _openInbox() async {
    await Clipboard.setData(ClipboardData(text: _inbox));
    _setStatus('收件箱路径已复制：$_inbox');
  }

  void _clearHistory() {
    setState(() => _received.clear());
  }

  PeerView? get _selectedPeer {
    for (final PeerView peer in _peers) {
      if (peer.instanceId == _selectedInstance) return peer;
    }
    return null;
  }

  double get _transferFraction {
    if (_progress.isEmpty) return -1;
    final List<TransferProgress> values =
        _progress.values.toList(growable: false);
    final int total = values.fold<int>(
        0, (int sum, TransferProgress item) => sum + item.total);
    final int done = values.fold<int>(
        0, (int sum, TransferProgress item) => sum + item.done);
    return total <= 0 ? 0 : (done / total).clamp(0, 1).toDouble();
  }

  double get _transferPercent {
    final double fraction = _transferFraction;
    return fraction < 0 ? -1.0 : fraction * 100;
  }

  String get _hintText {
    if (_starting) return '正在启动共享传输核心…';
    if (_sharedFiles.isNotEmpty && _selectedInstance == null) {
      return '有 ${_sharedFiles.length} 个待发送文件 · 点选下方设备立即发送';
    }
    if (_selectedInstance != null) {
      // 与桌面端 selectionNote 一致：选中设备后顺带说明走的是哪条通道。
      final PeerView? peer = _selectedPeer;
      final String routes = peer == null ? '' : peer.routes.join(' · ');
      if (routes.isEmpty) return '轻点墨洞选择文件';
      return '轻点墨洞选择文件 · $routes';
    }
    if (_peers.isNotEmpty) return '点选下方设备作为目标';
    return '等待附近的墨洞上线…';
  }

  // ---- 界面 ----

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: inkBgDark,
      body: SafeArea(
        child: Column(
          children: <Widget>[
            _buildTopBar(),
            Expanded(
              child: LayoutBuilder(
                builder: (BuildContext context, BoxConstraints constraints) {
                  final double diameter = (constraints.maxHeight * 0.38)
                      .clamp(130.0, 230.0)
                      .toDouble();
                  return Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 18),
                    child: Column(
                      children: <Widget>[
                        InkHoleHero(
                          diameter: diameter,
                          transferPercent: _transferPercent,
                          searching: _peers.isEmpty && !_starting,
                          onTap: _sending
                              ? null
                              : () => unawaited(_chooseAndSend()),
                        ),
                        _buildStatusLine(),
                        _buildHintLine(),
                        if (_peers.isNotEmpty) _buildDeviceRow(),
                        _buildReceivedHeader(),
                        Expanded(child: _buildReceivedBody()),
                      ],
                    ),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildTopBar() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 2),
      child: Row(
        children: <Widget>[
          const Text(
            '墨洞',
            style: TextStyle(
              color: inkTextPrimary,
              fontSize: 21,
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(width: 8),
          const Text(
            'InkHole',
            style: TextStyle(color: inkTextDim, fontSize: 12),
          ),
          const Spacer(),
          IconButton(
            tooltip: '重新搜索设备',
            onPressed: () => unawaited(_refresh()),
            icon: const Icon(Icons.refresh, color: inkTextSecondary),
          ),
          IconButton(
            tooltip: '跨网络传输',
            onPressed: () => unawaited(_showCrossNetwork()),
            icon: const Icon(Icons.public, color: inkTextSecondary),
          ),
          IconButton(
            tooltip: '设置',
            onPressed: () => unawaited(_openSettings()),
            icon: const Icon(Icons.settings, color: inkTextSecondary),
          ),
        ],
      ),
    );
  }

  Widget _buildStatusLine() {
    final String message = _error ?? _status;
    final Color color;
    if (_error != null) {
      color = inkDanger;
    } else if (_transferPercent >= 0) {
      color = inkTeal;
    } else {
      color = inkTextSecondary;
    }
    return SizedBox(
      height: 40,
      child: Center(
        child: AnimatedSwitcher(
          duration: const Duration(milliseconds: 220),
          child: Text(
            message,
            key: ValueKey<String>(message),
            textAlign: TextAlign.center,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(color: color, fontSize: 13),
          ),
        ),
      ),
    );
  }

  Widget _buildHintLine() {
    if (_sending) {
      return SizedBox(
        height: 34,
        child: Center(
          child: TextButton.icon(
            onPressed: () => unawaited(_cancelSending()),
            style: TextButton.styleFrom(
              foregroundColor: inkDanger,
              // 中断是破坏性操作，给一层淡红底把它从青色的常规操作里挑出来。
              backgroundColor: inkDanger.withValues(alpha: 0.12),
              padding: const EdgeInsets.symmetric(horizontal: 14),
              shape: const StadiumBorder(),
            ),
            icon: const Icon(Icons.close, size: 15),
            label: const Text('取消发送', style: TextStyle(fontSize: 12)),
          ),
        ),
      );
    }
    return SizedBox(
      height: 34,
      child: Center(
        child: Text(
          _hintText,
          textAlign: TextAlign.center,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(color: inkTextDim, fontSize: 11),
        ),
      ),
    );
  }

  Widget _buildDeviceRow() {
    return Padding(
      padding: const EdgeInsets.only(top: 4, bottom: 12),
      child: SizedBox(
        height: 64,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          itemCount: _peers.length,
          separatorBuilder: (BuildContext context, int index) =>
              const SizedBox(width: 8),
          itemBuilder: (BuildContext context, int index) {
            final PeerView peer = _peers[index];
            return Center(
              child: DeviceChip(
                peer: peer,
                selected: peer.instanceId == _selectedInstance,
                onTap: () => _onDeviceTap(peer),
              ),
            );
          },
        ),
      ),
    );
  }

  Widget _buildReceivedHeader() {
    return Row(
      children: <Widget>[
        const Text(
          '已接收',
          style: TextStyle(
            color: inkTextSecondary,
            fontSize: 13,
            fontWeight: FontWeight.w500,
          ),
        ),
        const Spacer(),
        if (_received.isNotEmpty)
          IconButton(
            tooltip: '清空记录',
            onPressed: _clearHistory,
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
            icon: const Icon(
              Icons.delete_sweep_outlined,
              color: inkTextDim,
              size: 17,
            ),
          ),
        TextButton.icon(
          onPressed: () => unawaited(_openInbox()),
          icon: const Icon(Icons.folder_outlined, color: inkTeal, size: 15),
          label: const Text(
            '收件箱',
            style: TextStyle(color: inkTeal, fontSize: 12),
          ),
        ),
      ],
    );
  }

  Widget _buildReceivedBody() {
    if (_received.isEmpty) {
      return const Align(
        alignment: Alignment.topCenter,
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: Text(
            '还没有收到过文件',
            style: TextStyle(color: inkTextDim, fontSize: 12),
          ),
        ),
      );
    }
    return ListView.separated(
      padding: const EdgeInsets.only(bottom: 14),
      itemCount: _received.length,
      separatorBuilder: (BuildContext context, int index) =>
          const SizedBox(height: 6),
      itemBuilder: (BuildContext context, int index) {
        final ReceivedFile file = _received[index];
        return FileCard(
          file: file,
          onTap: () => unawaited(_openReceived(file)),
          onLongPress: () => _copyText(file.path, '已复制文件路径'),
        );
      },
    );
  }
}
