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

import 'core/inkhole_core.dart';
import 'models.dart';
import 'theme.dart';
import 'views/inbox_view.dart';
import 'views/pair_view.dart';
import 'views/radar_view.dart';
import 'views/settings_view.dart';
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

  SharedPreferences? _preferences;
  String? _sessionId;
  String? _identityPrivate;
  String _peerName = 'InkHole Phone';
  String _instanceId = '';
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

  String? _selectedInstance;
  String? _activeWormholeSession;
  String _currentPasscode = '7-starburst-hydra';
  bool _isGeneratingPasscode = false;
  bool _isTransferPaused = false;

  bool _encryptionEnabled = false;
  List<ManualPeer> _manualPeers = const <ManualPeer>[];
  List<PeerView> _peers = const <PeerView>[];

  final List<ReceivedFile> _received = <ReceivedFile>[];
  String _exportPath = '';
  final List<String> _sharedFiles = <String>[];
  final Map<String, TransferProgress> _progress = <String, TransferProgress>{};
  final Set<String> _sendIds = <String>{};

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
    _speedSamplerTimer?.cancel();
    _events?.cancel();
    _shareChannel.setMethodCallHandler(null);
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
    });
  }

  // ---- 系统与核心初始化 ----

  Future<void> _loadSharedFiles() async {
    try {
      final List<dynamic>? incoming =
          await _shareChannel.invokeListMethod<dynamic>('getSharedFiles');
      if (incoming != null && incoming.isNotEmpty) {
        _sharedFiles.addAll(incoming.map((dynamic p) => p.toString()));
      }
    } catch (_) {}
  }

  Future<dynamic> _onShareMethodCall(MethodCall call) async {
    if (call.method == 'onSharedFiles') {
      final List<dynamic>? files = call.arguments as List<dynamic>?;
      if (files != null && files.isNotEmpty) {
        final List<String> paths =
            files.map((dynamic p) => p.toString()).toList();
        _sharedFiles.addAll(paths);
        _toast('收到系统分享的 ${paths.length} 个文件');
      }
    }
    return null;
  }

  Future<void> _boot() async {
    _preferences = await SharedPreferences.getInstance();
    _instanceId = _loadInstanceId();
    _peerName = _preferences!.getString('peer_name') ?? 'InkHole Phone';
    _listenPort = _preferences!.getInt('listen_port') ?? 0;
    _encryptionEnabled = _preferences!.getBool('encryption_enabled') ?? false;
    _rendezvousUrl = _preferences!.getString('rendezvous_url') ?? '';
    _transitRelay = _preferences!.getString('transit_relay') ?? 'transit.magic-wormhole.io:4001';
    _sshEnabled = _preferences!.getBool('ssh_enabled') ?? false;
    _sshHost = _preferences!.getString('ssh_host') ?? '';
    _sshUser = _preferences!.getString('ssh_user') ?? '';
    _sshFingerprint = _preferences!.getString('ssh_fingerprint') ?? '';
    _sshPort = _preferences!.getInt('ssh_port') ?? 22;

    final List<String> rawPeers =
        _preferences!.getStringList('manual_peers') ?? const <String>[];
    _manualPeers = rawPeers.map(ManualPeer.decode).toList();

    _inbox = await _resolveInbox();
    final String customExport = _preferences!.getString('export_tree_label') ?? '';
    _exportPath = customExport.isNotEmpty ? customExport : _inbox;

    _identityPrivate = await _secureStorage.read(key: 'identity_private');

    _loadReceivedRecords();
    _events = _core.events.listen(_onCoreEvent);
    await _startLanSession();
    await _startSsh();

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

  Future<void> _startLanSession() async {
    final String secret =
        await _secureStorage.read(key: 'transfer_secret') ?? '';
    final Map<String, dynamic> request = <String, dynamic>{
      'instance_id': _instanceId,
      'name': _peerName,
      'listen_port': _listenPort,
      'inbox': _inbox,
      'manual_peers': _manualPeers
          .map((ManualPeer item) => <String, dynamic>{
                'name': item.name,
                'host': item.host,
                'port': item.port,
              })
          .toList(),
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

      final String? privateKey = result['identity_private']?.toString();
      if (privateKey != null && privateKey.isNotEmpty && privateKey != _identityPrivate) {
        _identityPrivate = privateKey;
        await _secureStorage.write(key: 'identity_private', value: privateKey);
      }
      _toast('局域网信标与 QUIC 监听已启动 (:$_actualPort)');
    } catch (e) {
      _toast('局域网启动异常: $e');
    }
  }

  Future<void> _startSsh() async {
    if (_sessionId == null || !_sshEnabled) return;
    final String privateKey =
        await _secureStorage.read(key: 'ssh_private_key') ?? '';
    final String passphrase =
        await _secureStorage.read(key: 'ssh_passphrase') ?? '';

    if (_sshHost.isEmpty || _sshUser.isEmpty || privateKey.isEmpty) return;

    try {
      final Map<String, dynamic> result =
          await _core.call('ssh.listen', <String, dynamic>{
        'session_id': _sessionId,
        'profile': <String, dynamic>{
          'id': 'mobile-default',
          'host': _sshHost,
          'port': _sshPort,
          'user': _sshUser,
          'private_key': privateKey,
          'private_key_label': 'InkHole Mobile Storage',
          'passphrase': passphrase,
          'host_key_sha256': _sshFingerprint,
        },
        'remote_port': 0,
        'peers': const <Map<String, dynamic>>[],
      });
      _sshSessionId = result['session_id']?.toString();
    } catch (_) {}
  }

  Future<void> _stopCore() async {
    if (_sshSessionId != null) {
      try {
        await _core.call('session.cancel', <String, dynamic>{
          'session_id': _sshSessionId,
        });
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

  // ---- 核心事件调度 ----

  void _onCoreEvent(Map<String, dynamic> event) {
    final String name = event['event']?.toString() ?? '';
    final Map<String, dynamic> data = Map<String, dynamic>.from(
      event['data'] as Map? ?? const <String, dynamic>{},
    );

    switch (name) {
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
          setState(() {
            _progress.remove(id);
            _sendIds.remove(id);
          });
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

      case 'wormhole.offer':
        _activeWormholeSession = data['session_id']?.toString();
        if (mounted) unawaited(_showWormholeOfferDialog(data));

      case 'wormhole.ready':
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

  Future<void> _pickAndSendFiles() async {
    PeerView? targetPeer;
    if (_selectedInstance != null) {
      try {
        targetPeer = _peers.firstWhere((PeerView p) => p.instanceId == _selectedInstance);
      } catch (_) {}
    }

    if (targetPeer == null && _peers.isNotEmpty) {
      targetPeer = _peers.first;
      setState(() => _selectedInstance = targetPeer!.instanceId);
    }

    if (targetPeer == null) {
      _toast('请先在雷达上选择或等待目标设备上线');
      return;
    }

    final FilePickerResult? result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      type: FileType.any,
    );

    if (result != null && result.paths.isNotEmpty) {
      final List<String> paths =
          result.paths.whereType<String>().toList(growable: false);
      await _sendToPeer(targetPeer, paths);
    }
  }

  Future<void> _sendToPeer(PeerView peer, List<String> paths) async {
    if (_sessionId == null) return;
    for (final String path in paths) {
      try {
        final Map<String, dynamic> res = await _core.call('lan.send', <String, dynamic>{
          'session_id': _sessionId,
          'target_instance_id': peer.instanceId,
          'path': path,
        });
        final String? sendId = res['send_id']?.toString();
        if (sendId != null) {
          setState(() {
            _sendIds.add(sendId);
          });
        }
      } catch (err) {
        _toast('发起传输失败: $err');
      }
    }
  }

  Future<void> _cancelActiveTransfer() async {
    if (_sendIds.isNotEmpty) {
      for (final String id in _sendIds.toList()) {
        try {
          await _core.call('lan.send.cancel', <String, dynamic>{
            'session_id': _sessionId,
            'send_id': id,
          });
        } catch (_) {}
      }
      setState(() {
        _sendIds.clear();
        _progress.clear();
      });
      _toast('已取消传输');
    }
  }

  Future<void> _generateWormholeCode() async {
    setState(() => _isGeneratingPasscode = true);
    try {
      final Map<String, dynamic> res = await _core.call('wormhole.create', <String, dynamic>{
        'rendezvous_url': _rendezvousUrl.isNotEmpty
            ? _rendezvousUrl
            : 'ws://relay.magic-wormhole.io:4000/v1',
        'transit_relay': _transitRelay,
      });
      final String? code = res['code']?.toString();
      if (code != null && code.isNotEmpty) {
        setState(() => _currentPasscode = code);
      }
    } catch (e) {
      _toast('生成暗号失败: $e');
    } finally {
      if (mounted) setState(() => _isGeneratingPasscode = false);
    }
  }

  Future<void> _joinWormholeCode(String code) async {
    _toast('正在连接暗号: $code');
    try {
      await _core.call('wormhole.join.start', <String, dynamic>{
        'code': code,
        'rendezvous_url': _rendezvousUrl.isNotEmpty
            ? _rendezvousUrl
            : 'ws://relay.magic-wormhole.io:4000/v1',
        'transit_relay': _transitRelay,
      });
    } catch (e) {
      _toast('加入短码失败: $e');
    }
  }

  Future<void> _showWormholeOfferDialog(Map<String, dynamic> offer) async {
    final String sender = offer['sender_name']?.toString() ?? '跨网墨洞对端';
    final String filename = offer['filename']?.toString() ?? '文件';
    final int size = asInt(offer['size']);

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
    unawaited(_generateWormholeCode());
    showDialog<void>(
      context: context,
      builder: (BuildContext ctx) {
        return WormholePairingDialog(
          passcode: _currentPasscode,
          isGenerating: _isGeneratingPasscode,
          onRefreshCode: _generateWormholeCode,
          onJoinCode: _joinWormholeCode,
        );
      },
    );
  }

  Future<void> _pickCustomDirectory() async {
    final String? selected = await FilePicker.platform.getDirectoryPath();
    if (selected != null && selected.isNotEmpty) {
      setState(() => _exportPath = selected);
      await _preferences!.setString('export_tree_label', selected);
      _toast('收件目录已更新为: $selected');
    }
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
          title: const Text('输入对端 IP / 域名连接', style: TextStyle(color: textPrimary, fontSize: 15)),
          content: TextField(
            controller: ipCtrl,
            style: const TextStyle(color: textPrimary, fontSize: 13, fontFamily: 'monospace'),
            decoration: const InputDecoration(
              hintText: '例如 192.168.1.108 或 100.x.x.x:41300',
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
                if (raw.isNotEmpty) {
                  final ManualPeer p = ManualPeer(name: '手动节点', host: raw);
                  setState(() => _manualPeers = <ManualPeer>[..._manualPeers, p]);
                  Navigator.of(ctx).pop();
                  _startLanSession();
                }
              },
              style: ElevatedButton.styleFrom(backgroundColor: jade400),
              child: const Text('连接', style: TextStyle(color: bgAbyss, fontWeight: FontWeight.bold)),
            ),
          ],
        );
      },
    );
  }

  Future<void> _exportReceived(String path) async {
    final File incoming = File(path);
    if (!await incoming.exists()) return;
    try {
      final Directory downloadDir = await getTemporaryDirectory();
      final String destPath = p.join(downloadDir.path, p.basename(path));
      await incoming.copy(destPath);
    } catch (_) {}
  }

  void _loadReceivedRecords() {
    final List<String> lines =
        _preferences!.getStringList('received_history') ?? const <String>[];
    _received.clear();
    for (final String line in lines) {
      final List<String> parts = line.split('|');
      if (parts.length >= 4) {
        _received.add(
          ReceivedFile(
            name: parts[0],
            path: parts[1],
            size: int.tryParse(parts[2]) ?? 0,
            receivedAt: DateTime.tryParse(parts[3]) ?? DateTime.now(),
            sender: parts.length >= 5 ? parts[4] : '',
          ),
        );
      }
    }
  }

  void _persistReceivedRecords() {
    final List<String> lines = _received.take(50).map((ReceivedFile f) {
      return '${f.name}|${f.path}|${f.size}|${f.receivedAt.toIso8601String()}|${f.sender}';
    }).toList();
    unawaited(_preferences!.setStringList('received_history', lines));
  }

  Future<void> _saveSettings(InkSettings settings) async {
    _peerName = settings.peerName;
    _listenPort = settings.listenPort;
    _transitRelay = settings.transitRelay;
    _manualPeers = settings.manualPeers;

    await _preferences!.setString('peer_name', _peerName);
    await _preferences!.setInt('listen_port', _listenPort);
    await _preferences!.setString('transit_relay', _transitRelay);
    await _preferences!.setStringList(
      'manual_peers',
      _manualPeers.map((ManualPeer p) => p.encode()).toList(),
    );

    // 重启局域网会话
    await _startLanSession();
  }

  void _openFile(ReceivedFile file) {
    _toast('已调用系统处理：${file.name}');
  }

  void _toast(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg),
        backgroundColor: surfaceContainer,
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
              identityFingerprint: _instanceId,
              peers: _peers,
              selectedPeerInstance: _selectedInstance,
              searching: _peers.isEmpty,
              transferPercent: transferPct,
              recentReceived: _received,
              onSelectPeer: (PeerView peer) {
                setState(() => _selectedInstance = peer.instanceId);
                _toast('已锁定设备: ${peer.name}');
              },
              onTapCore: _pickAndSendFiles,
              onSendFile: _pickAndSendFiles,
              onOpenWormhole: _openWormholeModal,
              onOpenPairQr: () => setState(() => _currentTabIndex = 3),
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
              onRefreshDiscovery: _startLanSession,
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
              onTogglePause: () => setState(() => _isTransferPaused = !_isTransferPaused),
              onCancelTransfer: _cancelActiveTransfer,
              onClearHistory: () {
                setState(() => _received.clear());
                _persistReceivedRecords();
              },
              onOpenFile: _openFile,
              onRefresh: _startLanSession,
              onOpenPairQr: () => setState(() => _currentTabIndex = 3),
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
            ),

            // Tab 2: 收件箱
            InboxView(
              inboxPath: _exportPath,
              files: _received,
              onOpenFile: _openFile,
              onBrowseDirectory: () => _toast('收件目录: $_exportPath'),
              onClearAll: () {
                setState(() => _received.clear());
                _persistReceivedRecords();
              },
              onRefresh: _startLanSession,
              onOpenPairQr: () => setState(() => _currentTabIndex = 3),
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
            ),

            // Tab 3: 配对与二维码
            PairView(
              listenPort: _actualPort,
              passcode: _currentPasscode,
              identityFingerprint: _instanceId,
              onBackToRadar: () => setState(() => _currentTabIndex = 0),
              onOpenSettings: () => setState(() => _currentTabIndex = 4),
              onManualIpConnect: _manualIpConnectDialog,
              onJoinWormholeCode: _joinWormholeCode,
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
              ),
              instanceId: _instanceId,
              actualPort: _actualPort,
              inboxPath: _exportPath,
              onBack: () => setState(() => _currentTabIndex = 0),
              onSave: _saveSettings,
              onChooseDirectory: _pickCustomDirectory,
              onResetDirectory: () => setState(() => _exportPath = _inbox),
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
