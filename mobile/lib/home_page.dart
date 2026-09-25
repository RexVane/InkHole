import 'dart:async';
import 'dart:convert';
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
import 'views/inbox_view.dart';
import 'views/pair_view.dart';
import 'views/radar_view.dart';
import 'views/settings_view.dart';
import 'views/ssh_relay_view.dart';
import 'views/transfers_view.dart';
import 'widgets/bottom_nav_bar.dart';
import 'widgets/wormhole_dialog.dart';

/// 墨洞 Cyber-Zen 移动端主界面容器 (Shell)
///
/// 真实连接单一 Rust 传输核心 (inkhole-core) 与 Isolate 异步事件：
/// - 局域网 QUIC 自动发现与对端选择
/// - 真实本地文件选择与流式发送
/// - 实时吞吐量平滑波形图采集
/// - 确定性断点续传与取消
/// - Magic Wormhole 一次性短码生成与穿透拉取
/// - 收件仓库落盘与记录持久化
/// - 设备参数修改与热重载
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

  int _currentTabIndex = 0;
  int _scanRequest = 0;

  SharedPreferences? _preferences;
  String? _sessionId;
  String? _identityPrivate;
  String _peerName = 'InkHole Phone';
  String _instanceId = '';
  String _certificateFingerprint = '';
  int _listenPort = 0;
  int _actualPort = 41300;
  String _inbox = '';
  String _rendezvousUrl = '';
  String _transitRelay = 'transit.magic-wormhole.io:4001';
  String _sshHost = '';
  String _sshUser = '';
  String _sshFingerprint = '';
  int _sshPort = 22;
  bool _sshEnabled = false;
  String? _sshSessionId;

  /// 远端监听端口，0 = 交给服务器自动分配。
  int _sshRemotePort = sshRemotePortAuto;

  /// SSH 中继控制台的实时状态。
  ///
  /// 用 ValueNotifier 而不是普通字段：控制台是 Navigator.push 出来的路由，
  /// 本页面 setState 不会重建它，只有可监听对象才能把核心事件推上去。
  final ValueNotifier<SshRelayStatus> _sshRelay =
      ValueNotifier<SshRelayStatus>(const SshRelayStatus());

  /// dispose 已开始：此后任何异步回写（_stopCore 等）不得再触碰
  /// [_sshRelay]，否则是 use-after-dispose。
  bool _teardown = false;

  String? _selectedInstance;
  String? _activeWormholeSession;
  String _currentPasscode = '';
  DateTime? _passcodeExpiresAt;
  bool _isGeneratingPasscode = false;
  bool _isTransferPaused = false;
  bool _pauseInProgress = false;

  bool _encryptionEnabled = false;
  List<ManualPeer> _manualPeers = const <ManualPeer>[];
  List<PeerView> _peers = const <PeerView>[];

  final List<ReceivedFile> _received = <ReceivedFile>[];
  String _exportPath = '';
  String _exportTreeUri = '';
  final List<String> _sharedFiles = <String>[];
  List<String> _pendingSendPaths = <String>[];
  String? _pendingSendHost;
  final Map<String, TransferProgress> _progress = <String, TransferProgress>{};
  final Set<String> _sendIds = <String>{};
  final List<_TrackedSend> _trackedSends = <_TrackedSend>[];
  final List<_TrackedSend> _pausedSends = <_TrackedSend>[];

  /// 传输批次代数。取消/暂停时 +1，用来让在途的 `lan.send` 调用在返回时
  /// 识别出"这批已经被取消"，从而不再把 send_id 写回状态。
  int _transferEpoch = 0;

  /// `_startLanSession` 的串行化闸门，避免并发启动互相顶掉会话。
  Future<void>? _lanSessionLock;

  // 传输速率平滑采样 (每秒将实时速率推入历史序列，供 ThroughputChart 渲染)
  String _speedKey = '';
  int _speedSampleTime = 0;
  int _speedSampleDone = 0;
  double _speedBytes = 0;
  final List<double> _speedHistory = <double>[];
  Timer? _speedSamplerTimer;

  @override
  void initState() {
    super.initState();
    _core = InkHoleCore();
    _shareChannel.setMethodCallHandler(_onShareMethodCall);
    _startSpeedSampler();
    unawaited(_loadSharedFiles());
    unawaited(_boot());
  }

  @override
  void dispose() {
    // 先立拆除标志再释放资源：_stopCore 是异步的，越过 await 之后
    // 仍可能回来写 _sshRelay，不能只靠调用顺序保证安全。
    _teardown = true;
    _speedSamplerTimer?.cancel();
    _events?.cancel();
    _shareChannel.setMethodCallHandler(null);
    _sshRelay.dispose();
    unawaited(_stopCore());
    super.dispose();
  }

  void _startSpeedSampler() {
    _speedSamplerTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      final double mbPerSec = _speedBytes / (1024 * 1024);
      setState(() {
        if (_speedHistory.length >= 30) {
          _speedHistory.removeAt(0);
        }
        _speedHistory.add(mbPerSec > 0 ? mbPerSec : 0.0);
      });
      // 传输结束后必须把速率清零，否则采样器会一直把最后一段陈旧速率
      // 推入图表，看起来像"传输完了还在跑"。
      if (_sendIds.isEmpty && _activeWormholeSession == null) {
        _speedKey = '';
        _speedBytes = 0;
        _speedSampleTime = 0;
        _speedSampleDone = 0;
      }
    });
  }

  // ---- 系统与核心初始化 ----

  Future<void> _loadSharedFiles() async {
    try {
      final List<dynamic>? incoming =
          await _shareChannel.invokeListMethod<dynamic>('consumeSharedFiles');
      if (incoming != null && incoming.isNotEmpty) {
        _sharedFiles.addAll(incoming.map((dynamic p) => p.toString()));
      }
      final List<dynamic>? errors =
          await _shareChannel.invokeListMethod<dynamic>('consumeShareErrors');
      if (errors != null && errors.isNotEmpty) {
        _toast('分享读取提示: ${errors.join(", ")}');
      }
    } catch (_) {}
  }

  Future<dynamic> _onShareMethodCall(MethodCall call) async {
    if (call.method == 'sharedFiles') {
      final List<dynamic>? files = call.arguments as List<dynamic>?;
      if (files != null && files.isNotEmpty) {
        final List<String> paths =
            files.map((dynamic p) => p.toString()).toList();
        _sharedFiles.addAll(paths);
        _toast('收到系统分享的 ${paths.length} 个文件');
      }
    } else if (call.method == 'shareError') {
      final String? error = call.arguments?.toString();
      if (error != null && error.isNotEmpty) {
        _toast('系统分享提示: $error');
      }
    }
    return null;
  }

  Future<void> _boot() async {
    _preferences = await SharedPreferences.getInstance();
    _instanceId = _loadInstanceId();
    _peerName = _preferences!.getString('peer_name') ?? 'InkHole Phone';
    _listenPort = _preferences!.getInt('listen_port') ?? quicDefaultPort;
    _encryptionEnabled = _preferences!.getBool('encryption_enabled') ?? false;
    _rendezvousUrl = normalizeRendezvousUrl(
      _preferences!.getString('rendezvous_url') ?? '',
    );
    _transitRelay = _preferences!.getString('transit_relay') ?? 'transit.magic-wormhole.io:4001';
    _sshEnabled = _preferences!.getBool('ssh_enabled') ?? false;
    _sshHost = _preferences!.getString('ssh_host') ?? '';
    _sshUser = _preferences!.getString('ssh_user') ?? '';
    _sshFingerprint = _preferences!.getString('ssh_fingerprint') ?? '';
    _sshPort = _preferences!.getInt('ssh_port') ?? 22;
    _sshRemotePort =
        _preferences!.getInt('ssh_remote_port') ?? sshRemotePortAuto;

    final List<String> rawPeers =
        _preferences!.getStringList('manual_peers') ?? const <String>[];
    _manualPeers = rawPeers.map(ManualPeer.decode).toList();

    _inbox = await _resolveInbox();
    final String customExport = _preferences!.getString('export_tree_label') ?? '';
    _exportPath = customExport.isNotEmpty ? customExport : _inbox;
    _exportTreeUri = _preferences!.getString('export_tree_uri') ?? '';

    _identityPrivate = await _secureStorage.read(key: 'identity_private');

    _loadReceivedRecords();
    _events = _core.events.listen(_onCoreEvent);
    await _startLanSession();

    if (mounted) setState(() {});
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

  Future<String> _resolveInbox() async {
    final Directory root = await getApplicationDocumentsDirectory();
    final Directory inbox = Directory(p.join(root.path, 'InkHole'));
    await inbox.create(recursive: true);
    return inbox.path;
  }

  /// 雷达"刷新发现"。
  ///
  /// 走核心的 `lan.refresh`(只重新广播一次 + 回读当前 peers)，而不是重启
  /// 整个 lan 会话——后者会 cancel 掉全部在途传输，用户点一下刷新就把
  /// 正在传的文件掐断了。
  Future<void> _refreshDiscovery() async {
    final String? session = _sessionId;
    if (session == null) {
      await _startLanSession();
      return;
    }
    try {
      final Map<String, dynamic> result =
          await _core.call('lan.refresh', <String, dynamic>{
        'session_id': session,
      });
      final List<dynamic> values =
          result['peers'] as List<dynamic>? ?? const <dynamic>[];
      if (!mounted) return;
      setState(() {
        _peers = values
            .whereType<Map<dynamic, dynamic>>()
            .map((Map<dynamic, dynamic> val) =>
                PeerView.fromJson(Map<String, dynamic>.from(val)))
            .toList(growable: false);
        if (_selectedInstance != null &&
            !_peers.any((PeerView p) => p.instanceId == _selectedInstance)) {
          _selectedInstance = null;
        }
      });
      _flushPendingSends();
    } catch (e) {
      _toast('刷新发现失败: $e');
    }
  }

  Future<void> _startLanSession() async {
    // 串行化:雷达刷新、保存配置、添加对端三处都会触发本方法。并发进入时
    // 后一次会覆盖 _sessionId，前一个会话就成了没人能停的孤儿——它继续
    // 占着 QUIC 端口并往 UI 推事件。这里排队执行，并在启动前先停旧会话。
    final Future<void> previous = _lanSessionLock ?? Future<void>.value();
    final Completer<void> gate = Completer<void>();
    _lanSessionLock = previous.then((_) => gate.future);
    await previous;
    try {
      await _startLanSessionLocked();
    } finally {
      if (!gate.isCompleted) gate.complete();
    }
  }

  Future<void> _startLanSessionLocked() async {
    // 停掉上一个会话，避免重复启动时端口被自己占住(Windows 上尤其明显)。
    final String? existing = _sessionId;
    if (existing != null) {
      _sessionId = null;
      try {
        await _core.call('lan.stop', <String, dynamic>{
          'session_id': existing,
        });
      } catch (_) {}
    }

    final String secret =
        await _secureStorage.read(key: 'transfer_secret') ?? '';
    final List<String> discoveryTargets = _manualPeers
        .map(discoveryTargetFor)
        .where((String target) => target.isNotEmpty)
        .toList();

    final Map<String, dynamic> request = <String, dynamic>{
      'peer_name': _peerName,
      'instance_id': _instanceId,
      'listen_port': _listenPort,
      'inbox': _inbox,
      'capabilities': <String>['quic-v2', 'blake3', 'folder-v1'],
      'discovery_targets': discoveryTargets,
      if (_identityPrivate != null && _identityPrivate!.isNotEmpty)
        'identity_private': _identityPrivate,
      if (_encryptionEnabled && secret.isNotEmpty) 'secret': secret,
    };

    try {
      final Map<String, dynamic> result =
          await _core.call('lan.start', request);
      _sessionId = result['session_id']?.toString();
      _actualPort = asInt(result['port']);
      if (_actualPort == 0) _actualPort = 41300;
      final String fingerprint = result['fingerprint']?.toString() ?? '';
      if (fingerprint.isNotEmpty && mounted) {
        setState(() => _certificateFingerprint = fingerprint);
      }

      final String? privateKey = result['identity_private']?.toString();
      if (privateKey != null && privateKey.isNotEmpty && privateKey != _identityPrivate) {
        _identityPrivate = privateKey;
        await _secureStorage.write(key: 'identity_private', value: privateKey);
      }
      await _startSsh();
      _toast('局域网信标与 QUIC 监听已启动 (:$_actualPort)');
    } catch (e) {
      _toast('局域网启动异常: $e');
    }
  }

  /// 启动时按已保存的配置自动拉起中继（受「SSH 反向隧道中继」开关控制）。
  Future<void> _startSsh() async {
    if (_sessionId == null || !_sshEnabled) return;
    final String privateKey =
        await _secureStorage.read(key: 'ssh_private_key') ?? '';
    final String passphrase =
        await _secureStorage.read(key: 'ssh_passphrase') ?? '';
    if (_sshHost.isEmpty || _sshUser.isEmpty || privateKey.isEmpty) return;
    await _listenSshRelay(
      host: _sshHost,
      port: _sshPort,
      user: _sshUser,
      fingerprint: _sshFingerprint,
      privateKey: privateKey,
      passphrase: passphrase,
      remotePort: _sshRemotePort,
      announce: false,
    );
  }

  /// 建立中继会话。
  ///
  /// 启动时的自动拉起与控制台的「保存并建立隧道」共用这一段，避免两条路径
  /// 行为漂移（历史上 `remote_port` 就被硬编码成 0，用户设的端口形同虚设）。
  Future<bool> _listenSshRelay({
    required String host,
    required int port,
    required String user,
    required String fingerprint,
    required String privateKey,
    required String passphrase,
    required int remotePort,
    bool announce = true,
  }) async {
    final String? session = _sessionId;
    if (session == null) {
      _toast('局域网会话未就绪，无法建立 SSH 中继');
      return false;
    }
    _sshRelay.value = _sshRelay.value.copyWith(
      starting: true,
      initialError: '',
      requestedRemotePort: remotePort,
    );
    _appendSshLog(
      '建立反向隧道 → $host:$port (user=$user, 远端端口 $remotePort)',
      SshLogLevel.info,
    );
    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.listen', <String, dynamic>{
        'session_id': session,
        'profile': <String, dynamic>{
          'id': 'mobile-default',
          'host': host,
          'port': port,
          'user': user,
          'private_key': privateKey,
          'private_key_label': 'InkHole Mobile Storage',
          'passphrase': passphrase,
          'host_key_sha256': fingerprint,
        },
        'remote_port': remotePort,
        'peers': const <Map<String, dynamic>>[],
      });
      _sshSessionId = result['session_id']?.toString();
      final int boundPort = asInt(result['remote_port']);
      // 非致命初始错误(认证被拒/端口占用等)由核心放在 result.error 里，
      // 会话本身仍然建立。必须提示用户，否则中继"开了但连不上"完全不可见。
      final Object? rawError = result['error'];
      final String initialError = rawError == null ? '' : '$rawError';
      _sshRelay.value = _sshRelay.value.copyWith(
        sessionId: _sshSessionId,
        starting: false,
        connected: result['connected'] == true,
        initialError: initialError,
        boundRemotePort: boundPort,
        identityPublicKey: result['noise_public']?.toString() ?? '',
        peers: SshPeerInfo.listFromJson(result['peers']),
        pendingPairCode: '',
      );
      _appendSshLog(
        '中继会话已创建 (${_sshSessionId ?? '无 id'})，远端端口 $boundPort',
        SshLogLevel.ok,
      );
      if (initialError.isNotEmpty) {
        _appendSshLog('未连通: $initialError', SshLogLevel.error);
        // 即使是从启动流程自动拉起也要提示：用户开着这个开关却连不上，
        // 这件事必须可见，否则中继"看起来开了、实际不通"完全无迹可寻。
        _toast('SSH 中继已启动但未连上: $initialError');
      } else if (announce) {
        _toast('SSH 中继已建立，远端端口 $boundPort');
      }
      return true;
    } catch (e) {
      _sshSessionId = null;
      _sshRelay.value = _sshRelay.value.copyWith(
        sessionId: null,
        starting: false,
        connected: false,
        initialError: '$e',
      );
      _appendSshLog('建立中继失败: $e', SshLogLevel.error);
      _toast('SSH 中继启动失败: $e');
      return false;
    }
  }

  /// 断开中继会话。核心的 `session.cancel` 会同时摘掉并关闭 relay 会话。
  Future<void> _stopSshRelay() async {
    final String? session = _sshSessionId;
    _sshSessionId = null;
    if (session != null) {
      try {
        final Map<String, dynamic> result =
            await _core.call('session.cancel', <String, dynamic>{
          'session_id': session,
        });
        _appendSshLog(
          result['cancelled'] == true ? '中继会话已断开' : '中继会话已不存在',
          SshLogLevel.info,
        );
      } catch (e) {
        _appendSshLog('断开中继失败: $e', SshLogLevel.error);
        _toast('断开 SSH 中继失败: $e');
      }
    }
    _sshRelay.value = _sshRelay.value.copyWith(
      sessionId: null,
      connected: false,
      starting: false,
      boundRemotePort: 0,
      peers: const <SshPeerInfo>[],
      pendingPairCode: '',
    );
  }

  /// 跑一次 `ssh.check`：真实连接 + 认证 + 取主机指纹，并实测耗时。
  Future<SshHandshakeResult?> _checkSshHandshake(InkSettings draft) async {
    _appendSshLog(
      '测试握手 → ${draft.sshHost}:${draft.sshPort} (user=${draft.sshUser})',
      SshLogLevel.info,
    );
    final Stopwatch watch = Stopwatch()..start();
    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.check', <String, dynamic>{
        'profile': <String, dynamic>{
          'id': 'mobile-default',
          'host': draft.sshHost,
          'port': draft.sshPort,
          'user': draft.sshUser,
          'private_key': draft.sshPrivateKey,
          'private_key_label': 'InkHole Mobile Storage',
          'passphrase': draft.sshPassphrase,
          'host_key_sha256': draft.sshFingerprint,
        },
      });
      watch.stop();
      final SshHandshakeResult handshake =
          SshHandshakeResult.fromJson(result, elapsed: watch.elapsed);
      _appendSshLog(
        '握手成功 ${handshake.elapsedLabel} · ${handshake.serverLabel}',
        SshLogLevel.ok,
      );
      _appendSshLog('主机指纹 ${handshake.fingerprint}', SshLogLevel.info);
      return handshake;
    } catch (e) {
      watch.stop();
      _appendSshLog('握手失败: $e', SshLogLevel.error);
      _toast('SSH 握手失败: $e');
      return null;
    }
  }

  Future<void> _saveSshFingerprint(String fingerprint) async {
    _sshFingerprint = fingerprint.trim();
    await _preferences?.setString('ssh_fingerprint', _sshFingerprint);
    _appendSshLog('已固定主机指纹 $_sshFingerprint', SshLogLevel.ok);
  }

  /// 控制台的「保存并建立隧道」。
  Future<void> _connectSshRelay(InkSettings draft) async {
    final String? invalid = validateSshRelayConfig(
      host: draft.sshHost,
      port: draft.sshPort,
      user: draft.sshUser,
      fingerprint: draft.sshFingerprint,
      privateKey: draft.sshPrivateKey,
      remotePort: draft.sshRemotePort,
    );
    if (invalid != null) {
      _toast(invalid);
      return;
    }
    await _saveSshSettings(draft);
    // 中继必须挂在 LAN 会话上；用户可能直接从控制台进来而没启动过局域网。
    if (_sessionId == null) await _startLanSession();
    if (_sessionId == null) {
      _toast('局域网会话未就绪，无法建立 SSH 中继');
      return;
    }
    if (_sshSessionId != null) await _stopSshRelay();
    await _listenSshRelay(
      host: draft.sshHost.trim(),
      port: draft.sshPort,
      user: draft.sshUser.trim(),
      fingerprint: draft.sshFingerprint.trim(),
      privateKey: draft.sshPrivateKey,
      passphrase: draft.sshPassphrase,
      remotePort: draft.sshRemotePort,
    );
  }

  /// 持久化控制台里改动的 SSH 配置（含私钥与口令，写安全存储）。
  Future<void> _saveSshSettings(InkSettings draft) async {
    final SharedPreferences? preferences = _preferences;
    if (preferences == null) return;
    _sshEnabled = draft.sshEnabled;
    _sshHost = draft.sshHost.trim();
    _sshUser = draft.sshUser.trim();
    _sshFingerprint = draft.sshFingerprint.trim();
    _sshPort = draft.sshPort == 0 ? 22 : draft.sshPort;
    _sshRemotePort = draft.sshRemotePort;
    await preferences.setBool('ssh_enabled', _sshEnabled);
    await preferences.setString('ssh_host', _sshHost);
    await preferences.setString('ssh_user', _sshUser);
    await preferences.setString('ssh_fingerprint', _sshFingerprint);
    await preferences.setInt('ssh_port', _sshPort);
    await preferences.setInt('ssh_remote_port', _sshRemotePort);
    if (draft.sshPrivateKey.isNotEmpty) {
      await _secureStorage.write(
        key: 'ssh_private_key',
        value: draft.sshPrivateKey,
      );
    }
    if (draft.sshPassphrase.isNotEmpty) {
      await _secureStorage.write(
        key: 'ssh_passphrase',
        value: draft.sshPassphrase,
      );
    }
    _appendSshLog(
      '配置已保存 ($_sshHost:$_sshPort, 远端端口 $_sshRemotePort)',
      SshLogLevel.info,
    );
  }

  /// 生成一次性配对码；核心会校验已有码是否仍在进行中。
  Future<void> _createSshPairCode() async {
    final String? session = _sshSessionId;
    if (session == null) {
      _toast('请先建立隧道');
      return;
    }
    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.pair.create', <String, dynamic>{
        'session_id': session,
      });
      final String code = result['code']?.toString() ?? '';
      if (code.isEmpty) {
        _appendSshLog('核心未返回配对码', SshLogLevel.error);
        return;
      }
      _sshRelay.value = _sshRelay.value.copyWith(pendingPairCode: code);
      _appendSshLog(
        '已生成一次性配对码（10 分钟内有效，成功即作废，失败超 5 次作废）',
        SshLogLevel.ok,
      );
    } catch (e) {
      _appendSshLog('生成配对码失败: $e', SshLogLevel.error);
      _toast('生成配对码失败: $e');
    }
  }

  /// 用对端给的配对码发起配对。成功后核心还会补一条 `ssh.paired` 事件，
  /// 对端身份由那条事件写入列表（按 instance_id 去重），这里不重复入列。
  Future<void> _joinSshPairCode(String code) async {
    final String? session = _sshSessionId;
    if (session == null) {
      _toast('请先建立隧道');
      return;
    }
    _appendSshLog('用配对码向对端发起握手', SshLogLevel.info);
    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.pair.join', <String, dynamic>{
        'session_id': session,
        'code': code,
      });
      final SshPeerInfo? peer = result['peer'] is Map
          ? SshPeerInfo.fromJson(
              Map<String, dynamic>.from(result['peer'] as Map),
            )
          : null;
      _appendSshLog(
        peer == null
            ? '配对完成'
            : '配对完成: ${peer.displayName} · ID ${peer.shortId}',
        SshLogLevel.ok,
      );
      _toast('配对完成');
    } catch (e) {
      _appendSshLog('配对失败: $e', SshLogLevel.error);
      _toast('配对失败: $e');
    }
  }

  /// 诊断终端只保留最近若干条：它同时是"最近发生了什么"的滚动视图，
  /// 不设上限会在长时间运行的隧道里无限增长。
  static const int _sshMaxLogLines = 200;

  void _appendSshLog(String text, SshLogLevel level) {
    if (!mounted) return;
    final List<SshLogLine> lines = <SshLogLine>[
      ..._sshRelay.value.logs,
      SshLogLine(at: DateTime.now(), text: text, level: level),
    ];
    _sshRelay.value = _sshRelay.value.copyWith(
      logs: lines.length > _sshMaxLogLines
          ? lines.sublist(lines.length - _sshMaxLogLines)
          : lines,
    );
  }

  void _clearSshLogs() {
    _sshRelay.value = _sshRelay.value.copyWith(logs: const <SshLogLine>[]);
  }

  /// 打开 SSH 中继控制台。
  Future<void> _openSshRelay() async {
    final String privateKey =
        await _secureStorage.read(key: 'ssh_private_key') ?? '';
    final String passphrase =
        await _secureStorage.read(key: 'ssh_passphrase') ?? '';
    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (BuildContext routeContext) => SshRelayView(
          initial: InkSettings(
            peerName: _peerName,
            listenPort: _listenPort,
            encryptionEnabled: _encryptionEnabled,
            secret: '',
            manualPeers: _manualPeers,
            rendezvousUrl: _rendezvousUrl,
            transitRelay: _transitRelay,
            sshEnabled: _sshEnabled,
            sshHost: _sshHost,
            sshPort: _sshPort,
            sshUser: _sshUser,
            sshFingerprint: _sshFingerprint,
            sshPrivateKey: privateKey,
            sshPassphrase: passphrase,
            sshRemotePort: _sshRemotePort,
          ),
          localPort: _actualPort,
          relay: _sshRelay,
          onBack: () => Navigator.of(routeContext).maybePop(),
          onCheckHandshake: _checkSshHandshake,
          onSaveFingerprint: _saveSshFingerprint,
          onConnect: _connectSshRelay,
          onDisconnect: _stopSshRelay,
          onCreatePairCode: _createSshPairCode,
          onJoinPairCode: _joinSshPairCode,
          onClearLogs: _clearSshLogs,
        ),
      ),
    );
  }

  Future<void> _stopCore() async {
    if (_sshSessionId != null) {
      try {
        await _core.call('session.cancel', <String, dynamic>{
          'session_id': _sshSessionId,
        });
      } catch (_) {}
      _sshSessionId = null;
    }
    // 核心一旦关停，中继会话在原生侧已经不存在，UI 状态必须跟着复位，
    // 否则控制台会一直显示一个已经不存在的 session_id。
    // 页面拆除中（dispose 触发的停机）时 Notifier 已释放，跳过复位。
    if (!_teardown) {
      _sshRelay.value = _sshRelay.value.copyWith(
        sessionId: null,
        connected: false,
        starting: false,
        boundRemotePort: 0,
        peers: const <SshPeerInfo>[],
        pendingPairCode: '',
      );
    }
    if (_sessionId != null) {
      try {
        await _core.call('lan.stop', <String, dynamic>{
          'session_id': _sessionId,
        });
      } catch (_) {}
    }
    // 必须投递 shutdown:只有走到 worker 的 shutdown 分支才会依次调用
    // native close() + destroy()，释放 tokio runtime 与 QUIC/UDP 监听器。
    // 直接 kill isolate 会把这些原生资源永久留在进程里，导致下次启动
    // 绑不上 41300/41301，且旧核心仍在后台收文件。
    await _core.dispose();
    if (_core.shutdownTimedOut) {
      // 原生核心没走完 destroy：残留监听端口，提示用户重启应用。
      _toast('核心未能完全退出，如无法发现设备请重启应用');
    }
  }

  // ---- 核心事件调度 ----

  void _onCoreEvent(Map<String, dynamic> event) {
    final String name = event['event']?.toString() ?? '';
    final Map<String, dynamic> data = Map<String, dynamic>.from(
      event['data'] as Map? ?? const <String, dynamic>{},
    );

    switch (name) {
      case 'core.fatal':
        // 原生核心崩溃/轮询失败:必须显式告知用户，否则表现为"应用还在，
        // 但什么也不传了"。同时清掉会话态，避免 UI 停留在假的"已连接"。
        final String reason = data['message']?.toString() ?? '未知错误';
        if (!mounted) return;
        setState(() {
          _peers = const <PeerView>[];
          _selectedInstance = null;
          _sessionId = null;
          _progress.clear();
          _sendIds.clear();
        });
        _toast('传输核心已停止: $reason');

      case 'lan.peers':
        final List<dynamic> values =
            data['peers'] as List<dynamic>? ?? const <dynamic>[];
        if (!mounted) return;
        setState(() {
          _peers = values
              .whereType<Map<dynamic, dynamic>>()
              .map((Map<dynamic, dynamic> val) =>
                  PeerView.fromJson(Map<String, dynamic>.from(val)))
              .toList(growable: false);
          if (_selectedInstance != null &&
              !_peers.any((PeerView p) => p.instanceId == _selectedInstance)) {
            _selectedInstance = null;
          }
        });
        _flushPendingSends();

      case 'lan.progress':
        final String key = data['send_id']?.toString() ??
            data['transfer_id']?.toString() ??
            name;
        final String filename = data['filename']?.toString() ?? '文件';
        final int done = asInt(data['done']);
        final int total = asInt(data['total']);
        _calculateSpeed(data['kind']?.toString() ?? 'send', filename, done, total);

        if (!mounted) return;
        setState(() {
          _progress[key] = TransferProgress(filename, done, total);
        });

      case 'lan.sent':
        final String? id = data['send_id']?.toString();
        if (id != null) {
          if (!mounted) return;
          final bool paused = _pauseInProgress ||
              _pausedSends.any((_TrackedSend job) => job.sendId == id);
          setState(() {
            _progress.remove(id);
            _sendIds.remove(id);
            _trackedSends.removeWhere((_TrackedSend job) => job.sendId == id);
          });
          if (paused && data['ok'] != true) return;
          _toast(data['ok'] == true ? '发送成功，校验完成' : '发送中断：${data['error'] ?? '网络异常'}');
        }

      case 'lan.received':
        final String path = data['path']?.toString() ?? '';
        final String? receiveId =
            data['send_id']?.toString() ?? data['transfer_id']?.toString();
        if (!mounted) return;
        setState(() {
          if (receiveId != null) _progress.remove(receiveId);
          _received.insert(
            0,
            ReceivedFile(
              name: data['filename']?.toString() ?? p.basename(path),
              path: path,
              size: asInt(data['size']),
              receivedAt: DateTime.now(),
              sender: data['sender_name']?.toString() ?? '未知对端',
            ),
          );
        });
        _persistReceivedRecords();
        if (path.isNotEmpty) {
          unawaited(_exportReceived(path));
        }

      case 'ssh.paired':
        // 核心在 PAKE 成功后直接把对端写进受信列表、没有二次确认，
        // 所以这里把对端身份同时落进状态和诊断日志，让用户能核对。
        final SshPeerInfo? pairedPeer = data['peer'] is Map
            ? SshPeerInfo.fromJson(
                Map<String, dynamic>.from(data['peer'] as Map),
              )
            : null;
        if (pairedPeer != null) {
          final SshRelayStatus current = _sshRelay.value;
          _sshRelay.value = current.copyWith(
            peers: <SshPeerInfo>[
              ...current.peers.where(
                (SshPeerInfo existing) =>
                    existing.instanceId != pairedPeer.instanceId,
              ),
              pairedPeer,
            ],
          );
        }
        final String label = pairedPeer?.displayName ?? '对端';
        _appendSshLog(
          pairedPeer == null
              ? '配对成功: $label'
              : '配对成功: $label · ID ${pairedPeer.shortId} · R:${pairedPeer.remotePort}',
          SshLogLevel.ok,
        );
        _toast('SSH 中继配对成功: $label');

      case 'ssh.connected':
        final int remotePort = asInt(data['remote_port']);
        _sshRelay.value = _sshRelay.value.copyWith(
          connected: true,
          starting: false,
          boundRemotePort: remotePort,
        );
        _appendSshLog('反向转发已就绪: 远端端口 $remotePort', SshLogLevel.ok);
        _toast('SSH 中继已连接(远端端口 $remotePort)');

      case 'ssh.reconnect.error':
        final String reconnectError = '${data['error'] ?? '连接中断'}';
        _sshRelay.value = _sshRelay.value.copyWith(
          connected: false,
          starting: true,
        );
        _appendSshLog('重连中: $reconnectError', SshLogLevel.warn);
        _toast('SSH 中继重连中: $reconnectError');

      case 'ssh.disconnected':
        final String disconnectError = '${data['error'] ?? '连接中断'}';
        _sshRelay.value = _sshRelay.value.copyWith(
          connected: false,
          starting: false,
        );
        _appendSshLog('连接断开: $disconnectError', SshLogLevel.warn);
        _toast('SSH 中继已断开: $disconnectError');

      case 'ssh.channel.error':
        final String channelError = '${data['error'] ?? '未知错误'}';
        _appendSshLog('通道错误: $channelError', SshLogLevel.error);
        _toast('SSH 中继通道错误: $channelError');

      case 'wormhole.offer':
        _activeWormholeSession = data['session_id']?.toString();
        final Map<String, dynamic> summary = data['summary'] is Map
            ? Map<String, dynamic>.from(data['summary'] as Map)
            : data;
        if (mounted) unawaited(_showWormholeOfferDialog(summary));

      case 'wormhole.ready':
        final List<dynamic> ids =
            data['send_ids'] as List<dynamic>? ?? const <dynamic>[];
        if (ids.isNotEmpty && mounted) {
          setState(() {
            for (final dynamic id in ids) {
              final String value = id.toString();
              if (value.isNotEmpty) _sendIds.add(value);
            }
          });
        }
        _toast('跨网络虫洞通道已建立');

      case 'wormhole.error':
        _activeWormholeSession = null;
        _toast('短码传输错误：${data['error']}');
    }
  }

  void _calculateSpeed(String kind, String filename, int done, int total) {
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
        _speedBytes = _speedBytes <= 0 ? instant : (_speedBytes * 0.65 + instant * 0.35);
        _speedSampleTime = now;
        _speedSampleDone = done;
      }
    }
  }

  // ---- 业务操作响应 ----

  void _flushPendingSends() {
    if (_pendingSendPaths.isEmpty || _peers.isEmpty) return;
    PeerView? peer;
    final String? host = _pendingSendHost;
    if (host == null || host.isEmpty) {
      peer = _peers.first;
    } else {
      for (final PeerView candidate in _peers) {
        if (sameEndpointHost(candidate.host, host)) {
          peer = candidate;
          break;
        }
      }
      if (peer == null) return;
    }
    final List<String> paths = List<String>.from(_pendingSendPaths);
    final PeerView target = peer;
    _pendingSendPaths = <String>[];
    _pendingSendHost = null;
    setState(() => _selectedInstance = target.instanceId);
    _toast('检测到设备 ${target.name} 上线，开始发送待发文件');
    unawaited(_sendToPeer(target, paths));
    setState(() => _currentTabIndex = 1);
  }

  void _rememberManualPeer(ManualPeer peer) {
    if (peer.host.isEmpty) return;
    setState(() {
      _manualPeers = <ManualPeer>[
        ..._manualPeers.where(
          (ManualPeer item) => !sameEndpointHost(item.host, peer.host),
        ),
        peer,
      ];
    });
    unawaited(_persistManualPeers());
    unawaited(_startLanSession());
  }

  Future<void> _persistManualPeers() async {
    final SharedPreferences? preferences = _preferences;
    if (preferences == null) return;
    await preferences.setStringList(
      'manual_peers',
      _manualPeers.map((ManualPeer peer) => peer.encode()).toList(),
    );
  }

  Map<String, dynamic> _wormholeSettings() => <String, dynamic>{
        'rendezvous_url': normalizeRendezvousUrl(_rendezvousUrl),
        'transit_relay': _transitRelay.trim().isEmpty
            ? 'transit.magic-wormhole.io:4001'
            : _transitRelay.trim(),
        'timeout_minutes': 10,
      };

  Future<void> _pickAndSendFiles() async {
    // 1. 如果系统分享队列中有待发文件，先弹窗询问
    if (_sharedFiles.isNotEmpty) {
      final bool? sendShared = await showDialog<bool>(
        context: context,
        builder: (BuildContext ctx) => AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Row(
            children: <Widget>[
              Icon(Icons.share, color: jade400, size: 20),
              SizedBox(width: 8),
              Text('发现分享文件', style: TextStyle(color: textPrimary, fontSize: 16)),
            ],
          ),
          content: Text(
            '检测到系统分享的 ${_sharedFiles.length} 个文件，是否立即发送？',
            style: const TextStyle(color: textMuted, fontSize: 13),
          ),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('重新选文件', style: TextStyle(color: textMuted)),
            ),
            ElevatedButton(
              onPressed: () => Navigator.of(ctx).pop(true),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
              ),
              child: const Text('发送分享文件',
                  style: TextStyle(color: bgAbyss, fontWeight: FontWeight.bold)),
            ),
          ],
        ),
      );
      if (sendShared == true) {
        final List<String> paths = List<String>.from(_sharedFiles);
        _sharedFiles.clear();
        await _dispatchSendPaths(paths);
        return;
      }
    }

    // 2. 正常调起系统文件选择器
    final FilePickerResult? result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      type: FileType.any,
    );

    if (result != null && result.paths.isNotEmpty) {
      final List<String> paths = result.paths
          .whereType<String>()
          .where((String p) => p.isNotEmpty)
          .toList(growable: false);
      if (paths.isNotEmpty) {
        await _dispatchSendPaths(paths);
      }
    }
  }

  Future<void> _dispatchSendPaths(List<String> paths) async {
    PeerView? targetPeer;
    if (_selectedInstance != null) {
      try {
        targetPeer =
            _peers.firstWhere((PeerView p) => p.instanceId == _selectedInstance);
      } catch (_) {}
    }

    if (targetPeer == null && _peers.isNotEmpty) {
      targetPeer = _peers.first;
      setState(() => _selectedInstance = targetPeer!.instanceId);
    }

    if (targetPeer != null) {
      await _sendToPeer(targetPeer, paths);
      setState(() => _currentTabIndex = 1);
      return;
    }

    // 局域网未探测到目标设备，弹出选择传输方案弹窗
    await _showNoPeerSendDialog(paths);
  }

  Future<void> _showNoPeerSendDialog(List<String> paths) async {
    await showDialog<void>(
      context: context,
      builder: (BuildContext ctx) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Row(
            children: <Widget>[
              Icon(Icons.radar, color: jade400, size: 20),
              SizedBox(width: 8),
              Text('选择传输方式', style: TextStyle(color: textPrimary, fontSize: 16)),
            ],
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Text(
                '已选择 ${paths.length} 个文件，当前雷达尚未探测到附近设备。您可以：',
                style: const TextStyle(color: textMuted, fontSize: 12),
              ),
              const SizedBox(height: 16),
              ElevatedButton.icon(
                onPressed: () {
                  Navigator.of(ctx).pop();
                  _createWormholeSend(paths);
                },
                icon: const Icon(Icons.key, size: 16, color: bgAbyss),
                label: const Text(
                  '生成一次性暗号 (跨网穿透)',
                  style: TextStyle(
                    color: bgAbyss,
                    fontSize: 12,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                style: ElevatedButton.styleFrom(
                  backgroundColor: jade400,
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10),
                  ),
                ),
              ),
              const SizedBox(height: 10),
              OutlinedButton.icon(
                onPressed: () {
                  Navigator.of(ctx).pop();
                  _manualIpSendDialog(paths);
                },
                icon: const Icon(Icons.settings_ethernet, size: 16, color: jade400),
                label: const Text(
                  '指定目标 IP 直连',
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: surfaceBorder),
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10),
                  ),
                ),
              ),
              const SizedBox(height: 10),
              OutlinedButton.icon(
                onPressed: () {
                  Navigator.of(ctx).pop();
                  setState(() {
                    _pendingSendPaths = paths;
                    _pendingSendHost = null;
                  });
                  _toast('已加入待发队列，附近设备上线后将自动发送');
                },
                icon: const Icon(Icons.schedule, size: 16, color: textMuted),
                label: const Text(
                  '加入待发队列 (等待设备上线)',
                  style: TextStyle(
                    color: textMuted,
                    fontSize: 12,
                  ),
                ),
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: surfaceBorder),
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }

  Future<void> _manualIpSendDialog(List<String> paths) async {
    final TextEditingController ipCtrl = TextEditingController();
    await showDialog<void>(
      context: context,
      builder: (BuildContext ctx) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Text('输入对端 IP / 域名',
              style: TextStyle(color: textPrimary, fontSize: 15)),
          content: TextField(
            controller: ipCtrl,
            style: const TextStyle(
                color: textPrimary, fontSize: 13, fontFamily: 'monospace'),
            decoration: const InputDecoration(
              hintText: '例如 192.168.1.108 或 100.64.0.2',
            ),
          ),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('取消', style: TextStyle(color: textMuted)),
            ),
            ElevatedButton(
              onPressed: () {
                final String raw = ipCtrl.text.trim();
                final ManualPeer peer =
                    parseManualEndpoint(raw, name: '目标设备');
                if (peer.host.isNotEmpty) {
                  setState(() {
                    _manualPeers = <ManualPeer>[
                      ..._manualPeers.where(
                        (ManualPeer item) =>
                            !sameEndpointHost(item.host, peer.host),
                      ),
                      peer,
                    ];
                    _pendingSendPaths = List<String>.from(paths);
                    _pendingSendHost = peer.host;
                  });
                  Navigator.of(ctx).pop();
                  unawaited(_persistManualPeers());
                  unawaited(_startLanSession());
                  _toast('正在发现 ${peer.host} 并准备发送...');
                }
              },
              style: ElevatedButton.styleFrom(backgroundColor: jade400),
              child: const Text('发现后发送',
                  style: TextStyle(color: bgAbyss, fontWeight: FontWeight.bold)),
            ),
          ],
        );
      },
    );
  }

  Future<void> _sendToPeer(PeerView peer, List<String> paths) async {
    if (_sessionId == null) {
      _toast('传输服务未就绪，正在重新启动...');
      await _startLanSession();
      if (_sessionId == null) {
        _toast('传输核心未就绪');
        return;
      }
    }
    // 记下本次批次的代数。取消/暂停会把 _transferEpoch 推进，
    // 在途的 lan.send 返回后据此判断"这批已经被取消了"，不再把 send_id
    // 塞回状态——否则取消后传输会"复活"，UI 与实际传输脱节。
    final int epoch = _transferEpoch;
    for (final String path in paths) {
      if (epoch != _transferEpoch) return;
      final _TrackedSend tracked = _TrackedSend(path: path, peer: peer);
      _trackedSends.add(tracked);
      try {
        final Map<String, dynamic> res =
            await _core.call('lan.send', <String, dynamic>{
          'session_id': _sessionId,
          'path': path,
          'instance_id': peer.instanceId,
          'host': peer.host,
          'port': peer.port,
          'fingerprint': peer.fingerprint,
        });
        final String? sendId = res['send_id']?.toString();
        if (sendId == null) continue;
        if (epoch != _transferEpoch) {
          // 取消/暂停发生在这次调用期间:刚建立的 send 要立刻撤掉，
          // 否则对端会继续收一个用户以为已经取消的文件。
          _trackedSends.remove(tracked);
          unawaited(_cancelSendQuietly(sendId));
          continue;
        }
        tracked.sendId = sendId;
        setState(() {
          _sendIds.add(sendId);
          _isTransferPaused = false;
        });
      } catch (err) {
        _trackedSends.remove(tracked);
        _toast('发起传输失败: $err');
      }
    }
  }

  /// 尽力撤销一个 send，不打扰用户(取消流程已经给过提示了)。
  Future<void> _cancelSendQuietly(String sendId) async {
    final String? session = _sessionId;
    if (session == null) return;
    try {
      await _core.call('lan.send.cancel', <String, dynamic>{
        'session_id': session,
        'send_id': sendId,
      });
    } catch (_) {}
  }

  Future<void> _cancelActiveTransfer() async {
    final bool hadWork = _sendIds.isNotEmpty ||
        _activeWormholeSession != null ||
        _pausedSends.isNotEmpty;
    // 先推进代数:让所有在途 lan.send 在返回时自行放弃。
    _transferEpoch++;
    for (final String id in _sendIds.toList()) {
      await _cancelSendQuietly(id);
    }
    final String? wormhole = _activeWormholeSession;
    if (wormhole != null) {
      try {
        await _core.call('session.cancel', <String, dynamic>{
          'session_id': wormhole,
        });
      } catch (_) {}
    }
    if (!hadWork) return;
    setState(() {
      _sendIds.clear();
      _trackedSends.clear();
      _pausedSends.clear();
      _progress.clear();
      _isTransferPaused = false;
      _pauseInProgress = false;
      if (wormhole != null) {
        _activeWormholeSession = null;
        _currentPasscode = '';
        _passcodeExpiresAt = null;
      }
    });
    _toast('已取消传输');
  }

  Future<void> _togglePause() async {
    if (_isTransferPaused) {
      final List<_TrackedSend> jobs = List<_TrackedSend>.of(_pausedSends);
      if (jobs.isEmpty) {
        setState(() {
          _isTransferPaused = false;
          _pauseInProgress = false;
        });
        _toast('没有可恢复的传输');
        return;
      }
      // 先清状态再发送。若反过来(先发后清)，发送过程中新增的 send_id 会被
      // 随后的 clear 抹掉，表现为"恢复了但列表里什么都没有"。
      setState(() {
        _isTransferPaused = false;
        _pauseInProgress = false;
        _pausedSends.clear();
      });
      final Map<String, List<String>> pathsByPeer = <String, List<String>>{};
      final Map<String, PeerView> peers = <String, PeerView>{};
      for (final _TrackedSend job in jobs) {
        pathsByPeer.putIfAbsent(job.peer.instanceId, () => <String>[]).add(job.path);
        peers[job.peer.instanceId] = job.peer;
      }
      int resumed = 0;
      for (final MapEntry<String, List<String>> entry in pathsByPeer.entries) {
        final PeerView? peer = peers[entry.key];
        if (peer == null) continue;
        // 对端可能已经离线:此时这批文件无处可去，必须明确告知用户，
        // 不能静默丢弃(旧实现用 toast 声称"已从断点恢复"但实际全丢)。
        final bool online =
            _peers.any((PeerView item) => item.instanceId == entry.key);
        if (!online) {
          final int lost = entry.value.length;
          _toast('${peer.name} 已离线，$lost 个文件未恢复，请重新选择对端');
          continue;
        }
        await _sendToPeer(peer, entry.value);
        resumed += entry.value.length;
      }
      if (resumed > 0) {
        // 恢复走的是整文件重发，能否续传取决于核心是否命中 .part，
        // 不要向用户承诺"断点续传"。
        _toast('已重新发起 $resumed 个文件，可续传部分将自动跳过');
      }
      return;
    }

    final List<_TrackedSend> lanJobs = _trackedSends
        .where((_TrackedSend job) =>
            job.sendId != null && _sendIds.contains(job.sendId))
        .toList(growable: false);
    if (lanJobs.isEmpty) {
      _toast(_sendIds.isNotEmpty || _activeWormholeSession != null
          ? '短码传输不能暂停，可点中止结束'
          : '当前没有进行中的传输');
      return;
    }
    setState(() => _pauseInProgress = true);
    // 推进代数:握手中尚未返回的 lan.send 回来后会自行撤销，
    // 避免"已暂停"状态下冒出新的 send_id 把暂停态翻回 false。
    _transferEpoch++;
    for (final _TrackedSend job in lanJobs) {
      await _cancelSendQuietly(job.sendId!);
    }
    if (!mounted) return;
    setState(() {
      _pausedSends
        ..clear()
        ..addAll(lanJobs);
      _isTransferPaused = true;
      _pauseInProgress = false;
      for (final _TrackedSend job in lanJobs) {
        _sendIds.remove(job.sendId);
        _trackedSends.remove(job);
        if (job.sendId != null) _progress.remove(job.sendId);
      }
    });
    _toast('已暂停，进度保存在断点里');
  }

  Future<void> _createWormholeSend(List<String> paths) async {
    if (_sessionId == null) {
      _toast('服务初始化中，请稍候');
      await _startLanSession();
      if (_sessionId == null) return;
    }
    setState(() => _isGeneratingPasscode = true);
    _toast('正在向中继注册一次性暗号...');
    try {
      final Map<String, dynamic> res =
          await _core.call('wormhole.create', <String, dynamic>{
        'session_id': _sessionId,
        'paths': paths,
        'settings': _wormholeSettings(),
      });
      final String? code = res['code']?.toString();
      if (code != null && code.isNotEmpty) {
        setState(() {
          _currentPasscode = code;
          _activeWormholeSession = res['session_id']?.toString();
          _passcodeExpiresAt = DateTime.tryParse(
            res['expires_at']?.toString() ?? '',
          );
        });
        if (mounted) {
          showDialog<void>(
            context: context,
            builder: (BuildContext ctx) {
              return WormholePairingDialog(
                passcode: code,
                isGenerating: false,
                onRefreshCode: () => _createWormholeSend(paths),
                onJoinCode: _joinWormholeCode,
                onPickFilesToSend: _pickFilesForWormhole,
                initialTab: 0,
                expiresAt: _passcodeExpiresAt,
              );
            },
          );
        }
      }
    } catch (e) {
      _toast('生成暗号失败: $e');
    } finally {
      if (mounted) setState(() => _isGeneratingPasscode = false);
    }
  }

  Future<void> _pickFilesForWormhole() async {
    if (_sharedFiles.isNotEmpty) {
      final List<String> shared = List<String>.from(_sharedFiles);
      _sharedFiles.clear();
      await _createWormholeSend(shared);
      return;
    }
    final FilePickerResult? result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      type: FileType.any,
    );
    if (result == null) return;
    final List<String> paths = result.paths
        .whereType<String>()
        .where((String path) => path.isNotEmpty)
        .toList(growable: false);
    if (paths.isEmpty) return;
    await _createWormholeSend(paths);
  }

  Future<void> _joinWormholeCode(String code) async {
    if (_sessionId == null) {
      _toast('服务初始化中，请稍候');
      await _startLanSession();
      if (_sessionId == null) return;
    }
    final String? parsed = parseScannedCode(code);
    final String trimmed = (parsed ?? code).trim();
    if (!looksLikeShortCode(trimmed)) {
      _toast('无法识别的暗号');
      return;
    }
    _toast('正在连接暗号: $trimmed');
    try {
      final Map<String, dynamic> joined =
          await _core.call('wormhole.join.start', <String, dynamic>{
        'session_id': _sessionId,
        'code': trimmed,
        'settings': _wormholeSettings(),
      });
      _activeWormholeSession = joined['session_id']?.toString();
      _toast('暗号请求已发送，等待对端握手');
      setState(() => _currentTabIndex = 1);
    } catch (e) {
      _toast('加入短码失败: $e');
    }
  }

  Future<void> _showWormholeOfferDialog(Map<String, dynamic> offer) async {
    final String sender = offer['device_name']?.toString() ??
        offer['sender_name']?.toString() ??
        '跨网墨洞对端';
    final List<String> names = (offer['names'] as List<dynamic>? ?? const <dynamic>[])
        .map((dynamic value) => value.toString())
        .where((String value) => value.isNotEmpty)
        .toList(growable: false);
    final int count = asInt(offer['item_count']);
    final String filename = names.isNotEmpty
        ? names.join('、')
        : (offer['filename']?.toString().isNotEmpty == true
            ? offer['filename'].toString()
            : (count > 0 ? '$count 个项目' : '文件'));
    final int summaryBytes = asInt(offer['total_bytes']);
    final int size = summaryBytes > 0 ? summaryBytes : asInt(offer['size']);

    final bool? accept = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Row(
            children: <Widget>[
              Icon(Icons.bolt, color: jade400, size: 20),
              SizedBox(width: 8),
              Text(
                '收到穿透传输请求',
                style: TextStyle(color: textPrimary, fontSize: 16),
              ),
            ],
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text('发送方：$sender', style: const TextStyle(color: textMuted, fontSize: 12)),
              const SizedBox(height: 6),
              Text('文件：$filename', style: const TextStyle(color: textPrimary, fontSize: 13, fontWeight: FontWeight.w600)),
              Text('大小：${formatBytes(size)}', style: const TextStyle(color: textMuted, fontSize: 12, fontFamily: 'monospace')),
            ],
          ),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('拒绝', style: TextStyle(color: dangerCoral)),
            ),
            ElevatedButton(
              onPressed: () => Navigator.of(ctx).pop(true),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
              ),
              child: const Text('接收', style: TextStyle(color: bgAbyss, fontWeight: FontWeight.bold)),
            ),
          ],
        );
      },
    );

    if (accept == true && _activeWormholeSession != null) {
      await _core.call('wormhole.accept', <String, dynamic>{
        'session_id': _activeWormholeSession,
      });
    } else if (_activeWormholeSession != null) {
      await _core.call('wormhole.reject', <String, dynamic>{
        'session_id': _activeWormholeSession,
      });
    }
  }

  void _openWormholeModal() {
    showDialog<void>(
      context: context,
      builder: (BuildContext ctx) {
        return WormholePairingDialog(
          passcode: _currentPasscode,
          isGenerating: _isGeneratingPasscode,
          onRefreshCode: _pickFilesForWormhole,
          onJoinCode: _joinWormholeCode,
          onPickFilesToSend: _pickFilesForWormhole,
          initialTab: 0,
          expiresAt: _passcodeExpiresAt,
        );
      },
    );
  }

  Future<void> _pickCustomDirectory() async {
    if (Platform.isAndroid) {
      try {
        final PickedDirectory? picked = await ExporterChannel.pickDirectory();
        if (picked == null) return;
        setState(() {
          _exportPath = picked.label;
          _exportTreeUri = picked.uri;
        });
        await _preferences!.setString('export_tree_label', picked.label);
        await _preferences!.setString('export_tree_uri', picked.uri);
        _toast('收件目录已更新为: ${picked.label}');
      } catch (e) {
        _toast('选择目录失败: $e');
      }
      return;
    }

    try {
      final String? selected = await FilePicker.platform.getDirectoryPath();
      if (selected != null && selected.isNotEmpty) {
        setState(() {
          _exportPath = selected;
          _exportTreeUri = '';
        });
        await _preferences!.setString('export_tree_label', selected);
        await _preferences!.remove('export_tree_uri');
        _toast('收件目录已更新为: $selected');
      }
    } catch (e) {
      _toast('选择目录失败: $e');
    }
  }

  Future<void> _resetExportDirectory() async {
    setState(() {
      _exportPath = _inbox;
      _exportTreeUri = '';
    });
    await _preferences?.remove('export_tree_label');
    await _preferences?.remove('export_tree_uri');
    _toast('收件目录已恢复为应用内收件箱');
  }

  Future<void> _manualIpConnectDialog() async {
    final TextEditingController ipCtrl = TextEditingController();
    await showDialog<void>(
      context: context,
      builder: (BuildContext ctx) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Text('添加对端地址', style: TextStyle(color: textPrimary, fontSize: 15)),
          content: TextField(
            controller: ipCtrl,
            style: const TextStyle(color: textPrimary, fontSize: 13, fontFamily: 'monospace'),
            decoration: const InputDecoration(
              hintText: '例如 192.168.1.108 或 100.64.0.2',
            ),
          ),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('取消', style: TextStyle(color: textMuted)),
            ),
            ElevatedButton(
              onPressed: () {
                final ManualPeer peer = parseManualEndpoint(
                  ipCtrl.text.trim(),
                  name: '手动节点',
                );
                if (peer.host.isNotEmpty) {
                  Navigator.of(ctx).pop();
                  _rememberManualPeer(peer);
                  _toast('正在发现 ${peer.host}');
                }
              },
              style: ElevatedButton.styleFrom(backgroundColor: jade400),
              child: const Text('加入发现', style: TextStyle(color: bgAbyss, fontWeight: FontWeight.bold)),
            ),
          ],
        );
      },
    );
  }

  Future<void> _exportReceived(String path) async {
    final File incoming = File(path);
    if (!await incoming.exists()) {
      _toast('文件已不在收件箱');
      return;
    }
    final ExportOutcome outcome = await ExporterChannel.export(
      path,
      treeUri: _exportTreeUri.isEmpty ? null : _exportTreeUri,
    );
    if (outcome.location.isNotEmpty) {
      _toast('已导出到 ${outcome.location}');
      return;
    }
    // 导出失败/平台不支持:文件仍留在应用内，必须明确告知，
    // 不能让用户以为已经导出成功。
    _toast('导出失败，文件仍保存在应用收件箱内');
  }

  void _loadReceivedRecords() {
    final List<String> lines =
        _preferences!.getStringList('received_history') ?? const <String>[];
    _received.clear();
    for (final String line in lines) {
      // 新格式是 JSON；旧格式是 `|` 拼接的 5 段文本，读到时顺带迁移。
      final ReceivedFile? parsed = _decodeReceivedRecord(line);
      if (parsed != null) _received.add(parsed);
    }
  }

  ReceivedFile? _decodeReceivedRecord(String line) {
    final String trimmed = line.trim();
    if (trimmed.startsWith('{')) {
      try {
        final Object? decoded = jsonDecode(trimmed);
        if (decoded is Map) {
          final Map<String, dynamic> map = Map<String, dynamic>.from(decoded);
          final String path = '${map['path'] ?? ''}';
          if (path.isEmpty) return null;
          return ReceivedFile(
            name: '${map['name'] ?? ''}',
            path: path,
            size: int.tryParse('${map['size'] ?? 0}') ?? 0,
            receivedAt: DateTime.tryParse('${map['received_at'] ?? ''}') ??
                DateTime.now(),
            sender: '${map['sender'] ?? ''}',
          );
        }
      } catch (_) {
        // 损坏的 JSON 记录直接跳过，不影响其余条目。
      }
      return null;
    }
    // 旧格式:文件名含 `|` 时字段会错位，只能按已知顺序尽力解析，
    // 之后由 _persistReceivedRecords 写成新格式。
    final List<String> parts = trimmed.split('|');
    if (parts.length < 4) return null;
    final String path = parts[1];
    if (path.isEmpty) return null;
    return ReceivedFile(
      name: parts[0],
      path: path,
      size: int.tryParse(parts[2]) ?? 0,
      receivedAt: DateTime.tryParse(parts[3]) ?? DateTime.now(),
      sender: parts.length >= 5 ? parts[4] : '',
    );
  }

  void _reloadReceivedRecords() {
    setState(_loadReceivedRecords);
  }

  void _persistReceivedRecords() {
    final List<String> lines = _received.take(50).map((ReceivedFile f) {
      // 用 JSON 而非 `|` 拼接:文件名可以合法包含 `|`，拼接会让字段错位。
      return jsonEncode(<String, dynamic>{
        'name': f.name,
        'path': f.path,
        'size': f.size,
        'received_at': f.receivedAt.toIso8601String(),
        'sender': f.sender,
      });
    }).toList();
    unawaited(_preferences!.setStringList('received_history', lines));
  }

  Future<void> _saveSettings(InkSettings settings) async {
    _peerName = settings.peerName.trim().isEmpty ? 'InkHole Phone' : settings.peerName.trim();
    _listenPort = settings.listenPort;
    _transitRelay = settings.transitRelay.trim().isEmpty
        ? 'transit.magic-wormhole.io:4001'
        : settings.transitRelay.trim();
    _manualPeers = settings.manualPeers
        .map((ManualPeer peer) => parseManualEndpoint(
              peer.port > 0
                  ? (peer.host.contains(':')
                      ? '[${peer.host}]:${peer.port}'
                      : '${peer.host}:${peer.port}')
                  : peer.host,
              name: peer.name.isEmpty ? '手动节点' : peer.name,
            ))
        .where((ManualPeer peer) => peer.host.isNotEmpty)
        .toList(growable: false);
    _rendezvousUrl = normalizeRendezvousUrl(settings.rendezvousUrl);
    _encryptionEnabled = settings.encryptionEnabled;
    _sshEnabled = settings.sshEnabled;
    _sshHost = settings.sshHost.trim();
    _sshUser = settings.sshUser.trim();
    _sshFingerprint = settings.sshFingerprint.trim();
    _sshPort = settings.sshPort == 0 ? 22 : settings.sshPort;
    _sshRemotePort = settings.sshRemotePort;

    final SharedPreferences preferences = _preferences!;
    await preferences.setString('peer_name', _peerName);
    await preferences.setInt('listen_port', _listenPort);
    await preferences.setString('transit_relay', _transitRelay);
    await preferences.setString('rendezvous_url', _rendezvousUrl);
    await preferences.setBool('encryption_enabled', _encryptionEnabled);
    await preferences.setBool('ssh_enabled', _sshEnabled);
    await preferences.setString('ssh_host', _sshHost);
    await preferences.setString('ssh_user', _sshUser);
    await preferences.setString('ssh_fingerprint', _sshFingerprint);
    await preferences.setInt('ssh_port', _sshPort);
    await preferences.setInt('ssh_remote_port', _sshRemotePort);
    await preferences.setStringList(
      'manual_peers',
      _manualPeers.map((ManualPeer peer) => peer.encode()).toList(),
    );
    if (settings.secret.isNotEmpty) {
      await _secureStorage.write(key: 'transfer_secret', value: settings.secret);
    }
    if (settings.sshPrivateKey.isNotEmpty) {
      await _secureStorage.write(
        key: 'ssh_private_key',
        value: settings.sshPrivateKey,
      );
    }
    if (settings.sshPassphrase.isNotEmpty) {
      await _secureStorage.write(
        key: 'ssh_passphrase',
        value: settings.sshPassphrase,
      );
    }

    await _startLanSession();
  }

  Future<void> _openFile(ReceivedFile file) async {
    try {
      await ExporterChannel.open(
        path: file.path,
        name: file.name,
        treeUri: _exportTreeUri.isEmpty ? null : _exportTreeUri,
      );
    } catch (e) {
      _toast('无法打开文件: $e');
    }
  }

  Future<void> _scanFromImage() async {
    if (!ScannerChannel.supportsImageScan) {
      _toast('当前平台暂不支持从相册识码');
      return;
    }
    try {
      final String? result = await ScannerChannel.scanImage();
      if (result != null && result.isNotEmpty) _handleScannedResult(result);
    } on PlatformException catch (error) {
      _toast(error.code == 'scan_not_found'
          ? '图片里没有识别到二维码'
          : '相册识码失败: ${error.message ?? error.code}');
    } on UnsupportedError {
      _toast('当前平台暂不支持从相册识码');
    } catch (e) {
      _toast('相册识码失败: $e');
    }
  }

  void _handleScannedResult(String raw) {
    final String? code = parseScannedCode(raw);
    if (code != null && looksLikeShortCode(code)) {
      unawaited(_joinWormholeCode(code));
      return;
    }
    final ManualPeer? endpoint = manualEndpointFromScan(raw);
    if (endpoint != null) {
      _rememberManualPeer(endpoint);
      _toast('已添加发现节点: ${endpoint.host}');
      return;
    }
    _toast('无法识别的二维码');
  }

  void _openScannerPairing() {
    setState(() {
      _currentTabIndex = 3;
      _scanRequest += 1;
    });
  }

  void _forgetPasscode() {
    if (_currentPasscode.isEmpty) return;
    // 暗号过期只是配对窗口结束，但会话本身还在核心侧跑着——不清掉
    // _activeWormholeSession，后续判定(中止/暂停/速度采样)都会以为还有
    // 一条活的短码传输，UI 与核心状态脱节。
    setState(() {
      _currentPasscode = '';
      _passcodeExpiresAt = null;
    });
    _toast('一次性暗号已过期');
  }

  void _toast(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          msg,
          style: const TextStyle(color: textPrimary, fontSize: 13),
        ),
        backgroundColor: surfaceContainer,
        behavior: SnackBarBehavior.floating,
        margin: const EdgeInsets.only(bottom: 76, left: 16, right: 16),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: const BorderSide(color: borderLuminescent),
        ),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  // ---- 页面构建 ----

  @override
  Widget build(BuildContext context) {
    TransferProgress? active;
    if (_progress.isNotEmpty) {
      active = _progress.values.first;
    }

    final double transferPct = active != null ? active.fraction * 100 : -1.0;

    return Scaffold(
      backgroundColor: bgAbyss,
      body: SafeArea(
        child: IndexedStack(
          index: _currentTabIndex,
          children: <Widget>[
            // Tab 0: 量子雷达
            RadarView(
              listenPort: _actualPort,
              identityFingerprint: _certificateFingerprint,
              peers: _peers,
              selectedPeerInstance: _selectedInstance,
              searching: _peers.isEmpty,
              transferPercent: transferPct,
              recentReceived: _received,
              onSelectPeer: (PeerView peer) {
                setState(() => _selectedInstance = peer.instanceId);
                _toast('已锁定设备: ${peer.name}');
              },
              onTapCore: () {
                if (_progress.isNotEmpty) {
                  setState(() => _currentTabIndex = 1);
                  return;
                }
                unawaited(_pickAndSendFiles());
              },
              onSendFile: _pickAndSendFiles,
              onOpenWormhole: _openWormholeModal,
              onOpenPairQr: _openScannerPairing,
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
              onRefreshDiscovery: () => unawaited(_refreshDiscovery()),
              onOpenInbox: () => setState(() => _currentTabIndex = 2),
              onOpenFile: _openFile,
            ),

            // Tab 1: 收发详情
            TransfersView(
              activeProgress: active,
              speedBytesPerSec: _speedBytes,
              speedHistory: _speedHistory,
              listenPort: _actualPort,
              receivedFiles: _received,
              isTransferPaused: _isTransferPaused,
              onTogglePause: () => unawaited(_togglePause()),
              onCancelTransfer: _cancelActiveTransfer,
              onClearHistory: () {
                setState(() => _received.clear());
                _persistReceivedRecords();
              },
              onOpenFile: _openFile,
              onRefresh: _reloadReceivedRecords,
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
            ),

            // Tab 2: 收件箱
            InboxView(
              inboxPath: _exportPath,
              files: _received,
              onOpenFile: _openFile,
              onBrowseDirectory: () {
                Clipboard.setData(ClipboardData(text: _exportPath));
                _toast('收件目录已复制。应用内文件请从下面的列表打开');
              },
              onClearAll: () {
                setState(() => _received.clear());
                _persistReceivedRecords();
              },
              onRefresh: _reloadReceivedRecords,
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
            ),

            // Tab 3: 配对与二维码
            PairView(
              listenPort: _actualPort,
              passcode: _currentPasscode,
              passcodeExpiresAt: _passcodeExpiresAt,
              identityFingerprint: _certificateFingerprint,
              onBackToRadar: () => setState(() => _currentTabIndex = 0),
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
              onManualIpConnect: _manualIpConnectDialog,
              cameraEnabled: _currentTabIndex == 3,
              onJoinWormholeCode: _joinWormholeCode,
              onScanned: _handleScannedResult,
              onScanImage: ScannerChannel.supportsImageScan
                  ? () => unawaited(_scanFromImage())
                  : null,
              onPickAndCreateWormhole: _pickFilesForWormhole,
              onPasscodeExpired: _forgetPasscode,
              scanRequest: _scanRequest,
            ),

            // Tab 4: 设置
            SettingsView(
              initialSettings: InkSettings(
                peerName: _peerName,
                listenPort: _listenPort,
                encryptionEnabled: _encryptionEnabled,
                secret: '',
                manualPeers: _manualPeers,
                rendezvousUrl: _rendezvousUrl,
                transitRelay: _transitRelay,
                sshEnabled: _sshEnabled,
                sshHost: _sshHost,
                sshPort: _sshPort,
                sshUser: _sshUser,
                sshFingerprint: _sshFingerprint,
                sshPrivateKey: '',
                sshPassphrase: '',
                sshRemotePort: _sshRemotePort,
              ),
              instanceId: _instanceId,
              actualPort: _actualPort,
              inboxPath: _exportPath,
              onBack: () => setState(() => _currentTabIndex = 0),
              onSave: _saveSettings,
              onChooseDirectory: _pickCustomDirectory,
              onResetDirectory: () => unawaited(_resetExportDirectory()),
              onOpenSshRelay: () => unawaited(_openSshRelay()),
            ),
          ],
        ),
      ),
      bottomNavigationBar: CyberBottomNavBar(
        currentIndex: _currentTabIndex,
        onTap: (int index) => setState(() => _currentTabIndex = index),
      ),
    );
  }
}

class _TrackedSend {
  _TrackedSend({required this.path, required this.peer});

  final String path;
  final PeerView peer;
  String? sendId;
}
