import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/updater.dart';
import '../models.dart';
import '../theme.dart';
import 'cross_network_dialog.dart';
import 'usage_guide_dialog.dart';

/// 把 PlatformException 压成用户可读的一句话。
String friendlyError(Object error) {
  if (error is PlatformException) {
    final String message = (error.message ?? '').trim();
    return message.isEmpty ? error.code : message;
  }
  return '$error';
}

/// 设置面板，对应旧版 MainActivity.kt#SettingsDialog。
///
/// 结构与旧版一致：本机信息 → 设备设置 → 存储 → 传输安全 →
/// 跨网络配置（Tailscale / 一次性短码 / SSH 中继 三标签）→ 帮助。
/// 保存时把整份草稿 pop 回页面，取消返回 null。
class SettingsDialog extends StatefulWidget {
  const SettingsDialog({
    super.key,
    required this.initial,
    required this.deviceLine,
    required this.portLine,
    required this.inboxPath,
    required this.exportPath,
    required this.onPickExportDirectory,
    required this.onResetExportDirectory,
    required this.sshReady,
    required this.onCheckSsh,
    required this.onCreateSshPair,
    required this.onOpenCrossNetwork,
    required this.onOpenRepository,
  });

  final InkSettings initial;
  final String deviceLine;
  final String portLine;

  /// 应用私有收件箱，导出失败时文件会留在这里。
  final String inboxPath;

  /// 收件落点的完整路径(默认目录的绝对路径，或解开的自定义目录)。
  final String exportPath;

  /// 弹系统目录选择器,选择成功返回新的完整路径,取消返回 null。
  final Future<String?> Function() onPickExportDirectory;

  /// 恢复默认收件目录,返回默认目录的完整路径。
  final Future<String> Function() onResetExportDirectory;

  final bool sshReady;

  /// 返回 ssh.check 的原始结果；失败时返回 null，错误由页面用状态栏提示。
  final Future<Map<String, dynamic>?> Function(InkSettings draft) onCheckSsh;

  final VoidCallback onCreateSshPair;
  final VoidCallback onOpenCrossNetwork;
  final VoidCallback onOpenRepository;

  @override
  State<SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<SettingsDialog> {
  late final TextEditingController _name;
  late final TextEditingController _listenPort;
  late final TextEditingController _secret;
  late final TextEditingController _rendezvous;
  late final TextEditingController _transit;
  late final TextEditingController _sshHost;
  late final TextEditingController _sshPort;
  late final TextEditingController _sshUser;
  late final TextEditingController _sshKey;
  late final TextEditingController _sshPassphrase;
  final TextEditingController _manualName = TextEditingController();
  final TextEditingController _manualHost = TextEditingController();
  final TextEditingController _manualPort = TextEditingController();

  late bool _encryption;
  late bool _sshEnabled;
  late String _fingerprint;
  late List<ManualPeer> _manualPeers;
  late bool _keyStored;
  late bool _passphraseStored;
  int _tab = 0;
  int _editing = -1;
  late String _exportPath;
  bool _checking = false;
  bool _checkingUpdate = false;
  String _manualError = '';
  String _error = '';

  @override
  void initState() {
    super.initState();
    final InkSettings initial = widget.initial;
    _name = TextEditingController(text: initial.peerName);
    _listenPort = TextEditingController(
      text: initial.listenPort == 0 ? '' : initial.listenPort.toString(),
    );
    _secret = TextEditingController(text: initial.secret);
    _rendezvous = TextEditingController(text: initial.rendezvousUrl);
    _transit = TextEditingController(text: initial.transitRelay);
    _sshHost = TextEditingController(text: initial.sshHost);
    _sshPort = TextEditingController(text: initial.sshPort.toString());
    _sshUser = TextEditingController(text: initial.sshUser);
    _sshKey = TextEditingController(text: initial.sshPrivateKey);
    _sshPassphrase = TextEditingController(text: initial.sshPassphrase);
    _encryption = initial.encryptionEnabled;
    _sshEnabled = initial.sshEnabled;
    _fingerprint = initial.sshFingerprint;
    _keyStored = initial.sshPrivateKey.isNotEmpty;
    _passphraseStored = initial.sshPassphrase.isNotEmpty;
    _manualPeers = List<ManualPeer>.of(initial.manualPeers);
    _exportPath = widget.exportPath;
  }

  @override
  void dispose() {
    _name.dispose();
    _listenPort.dispose();
    _secret.dispose();
    _rendezvous.dispose();
    _transit.dispose();
    _sshHost.dispose();
    _sshPort.dispose();
    _sshUser.dispose();
    _sshKey.dispose();
    _sshPassphrase.dispose();
    _manualName.dispose();
    _manualHost.dispose();
    _manualPort.dispose();
    super.dispose();
  }

  InkSettings _draft() {
    final String name = _name.text.trim();
    return InkSettings(
      peerName: name.length > 40 ? name.substring(0, 40) : name,
      listenPort: int.tryParse(_listenPort.text.trim()) ?? 0,
      encryptionEnabled: _encryption,
      secret: _secret.text.trim(),
      manualPeers: _manualPeers,
      rendezvousUrl: _rendezvous.text.trim(),
      transitRelay: _transit.text.trim(),
      sshEnabled: _sshEnabled,
      sshHost: _sshHost.text.trim(),
      sshPort: int.tryParse(_sshPort.text.trim()) ?? 22,
      sshUser: _sshUser.text.trim(),
      sshFingerprint: _fingerprint,
      sshPrivateKey: _sshKey.text.trim(),
      sshPassphrase: _sshPassphrase.text,
    );
  }

  /// 提交 Tailscale 标签页正在编辑的设备；输入非法返回 false 并留下提示。
  bool _submitManualPeer() {
    final String host = _manualHost.text.trim();
    if (host.isEmpty || host.contains(' ')) {
      setState(() => _manualError = 'Tailscale IP 或 MagicDNS 名称无效');
      return false;
    }
    final String portText = _manualPort.text.trim();
    final int manualPort =
        portText.isEmpty ? 0 : (int.tryParse(portText) ?? -1);
    if (manualPort < 0 || manualPort > 65535) {
      setState(() => _manualError = '对方发现 UDP 端口需为 1-65535 或留空');
      return false;
    }
    final ManualPeer peer = ManualPeer(
      name: _manualName.text.trim(),
      host: host,
      port: manualPort,
    );
    setState(() {
      _manualError = '';
      if (_editing >= 0 && _editing < _manualPeers.length) {
        _manualPeers[_editing] = peer;
      } else {
        _manualPeers.removeWhere((ManualPeer item) => item.host == host);
        _manualPeers.add(peer);
      }
      _editing = -1;
      _manualName.clear();
      _manualPort.clear();
      _manualHost.clear();
    });
    return true;
  }

  /// 填完地址直接点「保存」是最自然的操作，但设备要先经「添加设备」才进列表。
  /// 保存时兜底收下还留在输入框里的地址，否则用户以为存上了、回来却看不到。
  bool _commitPendingManualPeer() {
    if (_manualHost.text.trim().isEmpty) return true;
    return _submitManualPeer();
  }

  Future<void> _verifySsh() async {
    final int? port = int.tryParse(_sshPort.text.trim());
    if (port == null || port < 1 || port > 65535) {
      setState(() => _error = 'SSH 端口必须在 1-65535 范围内');
      return;
    }
    setState(() {
      _error = '';
      _checking = true;
    });
    final Map<String, dynamic>? result = await widget.onCheckSsh(_draft());
    if (!mounted) return;
    setState(() => _checking = false);
    if (result == null) return;
    final String fingerprint = result['fingerprint']?.toString() ?? '';
    if (fingerprint.isEmpty) return;
    final String? confirmed = await showDialog<String>(
      context: context,
      builder: (BuildContext dialogContext) => FingerprintConfirmDialog(
        fingerprint: fingerprint,
        serverVersion: result['server_version']?.toString() ?? '',
      ),
    );
    if (!mounted) return;
    if (confirmed != null && confirmed.isNotEmpty) {
      setState(() => _fingerprint = confirmed);
    }
  }

  void _save() {
    if (_encryption && _secret.text.trim().isEmpty) {
      setState(() => _error = '启用端到端加密后必须填写加密口令');
      return;
    }
    final String listenPortText = _listenPort.text.trim();
    final int? listenPort = int.tryParse(listenPortText);
    if (listenPortText.isNotEmpty &&
        (listenPort == null || listenPort < 1 || listenPort > 65535)) {
      setState(() => _error = '本机监听端口必须在 1-65535 范围内');
      return;
    }
    final int? sshPort = int.tryParse(_sshPort.text.trim());
    if (_sshEnabled && (sshPort == null || sshPort < 1 || sshPort > 65535)) {
      setState(() => _error = 'SSH 端口必须在 1-65535 范围内');
      return;
    }
    if (_name.text.trim().isEmpty) {
      setState(() => _error = '设备名称不能为空');
      return;
    }
    if (!_commitPendingManualPeer()) {
      setState(() {
        _tab = 0;
        _error = '请先修正 Tailscale 设备信息';
      });
      return;
    }
    Navigator.of(context).pop(_draft());
  }

  /// 在设置弹窗上层弹提示(SnackBar 会被弹窗遮挡且位置在屏幕底部)。
  Future<void> _showUpdateNotice(String title, String message) async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (BuildContext dialogContext) => AlertDialog(
        backgroundColor: inkBgCard,
        title: Text(title, style: const TextStyle(color: inkTextPrimary)),
        content: Text(
          message,
          style: const TextStyle(color: inkTextSecondary, fontSize: 13),
        ),
        actions: <Widget>[
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('好'),
          ),
        ],
      ),
    );
  }

  Future<void> _checkUpdate() async {
    setState(() => _checkingUpdate = true);
    UpdateInfo info;
    try {
      info = await UpdaterChannel.check(appVersion);
    } on Exception catch (error) {
      if (!mounted) return;
      setState(() => _checkingUpdate = false);
      await _showUpdateNotice('检查更新失败', friendlyError(error));
      return;
    }
    if (!mounted) return;
    setState(() => _checkingUpdate = false);
    if (!info.newer) {
      await _showUpdateNotice('检查更新', '当前已是最新版本 v$appVersion');
      return;
    }
    final bool? confirmed = await showDialog<bool>(
      context: context,
      builder: (BuildContext dialogContext) => AlertDialog(
        backgroundColor: inkBgCard,
        title: Text(
          '发现新版本 ${info.version}',
          style: const TextStyle(color: inkTextPrimary),
        ),
        content: Text(
          info.notes.isEmpty ? '新版本已发布，是否立即更新？' : info.notes,
          style: const TextStyle(color: inkTextSecondary, fontSize: 12),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: const Text('稍后'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: const Text('立即更新'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    if (info.apkUrl.isEmpty) {
      widget.onOpenRepository();
      return;
    }
    await _downloadAndInstall(info.apkUrl);
  }

  Future<void> _downloadAndInstall(String url) async {
    final ValueNotifier<int> progress = ValueNotifier<int>(0);
    UpdaterChannel.onProgress = (int percent) => progress.value = percent;
    bool dialogOpen = true;
    unawaited(
      showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (BuildContext dialogContext) => AlertDialog(
          backgroundColor: inkBgCard,
          title: const Text('正在下载更新', style: TextStyle(color: inkTextPrimary)),
          content: ValueListenableBuilder<int>(
            valueListenable: progress,
            builder: (BuildContext _, int percent, Widget? __) => Column(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                LinearProgressIndicator(
                  value: percent <= 0 ? null : percent / 100,
                ),
                const SizedBox(height: 10),
                Text(
                  percent <= 0 ? '连接中…' : '$percent%',
                  style: const TextStyle(color: inkTextSecondary, fontSize: 12),
                ),
              ],
            ),
          ),
        ),
      ).whenComplete(() => dialogOpen = false),
    );
    try {
      await UpdaterChannel.downloadInstall(url);
    } on Exception catch (error) {
      if (mounted && dialogOpen) {
        Navigator.of(context, rootNavigator: true).pop();
      }
      if (!mounted) return;
      await _showUpdateNotice('下载更新失败', friendlyError(error));
      return;
    } finally {
      UpdaterChannel.onProgress = null;
    }
    if (mounted && dialogOpen) Navigator.of(context, rootNavigator: true).pop();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: inkBgCard,
      // 对齐旧版:弹窗接近全屏,左右仅留窄边。
      insetPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 36),
      title: const Text('设置', style: TextStyle(color: inkTextPrimary)),
      content: SizedBox(
        width: double.maxFinite,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              SelectionArea(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(widget.deviceLine, style: _hintStyle),
                    Text('版本：v$appVersion', style: _hintStyle),
                    Text(widget.portLine, style: _hintStyle),
                  ],
                ),
              ),
              const SizedBox(height: 14),
              const _SectionTitle('设备设置'),
              const SizedBox(height: 6),
              TextField(
                controller: _name,
                decoration: const InputDecoration(labelText: '设备名称'),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _listenPort,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(
                  labelText: '本机监听端口（留空=自动）',
                ),
              ),
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text(
                  '局域网传输无需设置，保持自动即可；仅 Tailscale 直连时需要固定端口',
                  style: TextStyle(color: inkTextDim, fontSize: 11),
                ),
              ),
              const SizedBox(height: 14),
              const _SectionTitle('存储'),
              const SizedBox(height: 4),
              const Text(
                '收件目录',
                style: TextStyle(color: inkTextPrimary, fontSize: 13),
              ),
              SelectionArea(child: Text(_exportPath, style: _hintStyle)),
              const Text(
                '所有接收文件和文件夹统一保存在这里；未自定义时保存到系统下载目录的 InkHole 文件夹',
                style: TextStyle(color: inkTextDim, fontSize: 11),
              ),
              SelectionArea(
                child: Text(
                  '导出失败时暂存于应用内：${widget.inboxPath}',
                  style: const TextStyle(color: inkTextDim, fontSize: 11),
                ),
              ),
              Row(
                children: <Widget>[
                  TextButton(
                    onPressed: () async {
                      final String? path = await widget.onPickExportDirectory();
                      if (path != null && mounted) {
                        setState(() => _exportPath = path);
                      }
                    },
                    child: const Text('选择目录'),
                  ),
                  TextButton(
                    onPressed: () async {
                      final String path = await widget.onResetExportDirectory();
                      if (mounted) setState(() => _exportPath = path);
                    },
                    child: const Text('恢复默认'),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              const _SectionTitle('传输安全'),
              const SizedBox(height: 6),
              TextField(
                controller: _secret,
                enabled: _encryption,
                obscureText: true,
                decoration: const InputDecoration(labelText: '加密口令 (两端一致)'),
              ),
              const SizedBox(height: 8),
              Row(
                children: <Widget>[
                  const Expanded(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Text(
                          '端到端加密',
                          style: TextStyle(color: inkTextPrimary, fontSize: 14),
                        ),
                        Text(
                          '使用 AES-256-GCM 保护传输内容，两端需使用相同口令',
                          style: TextStyle(color: inkTextDim, fontSize: 11),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 8),
                  Switch(
                    value: _encryption,
                    onChanged: (bool value) =>
                        setState(() => _encryption = value),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              const _SectionTitle('跨网络配置'),
              const SizedBox(height: 6),
              _tabBar(),
              const SizedBox(height: 10),
              if (_tab == 0) ..._tailscaleTab(),
              if (_tab == 1) ..._wormholeTab(),
              if (_tab == 2) ..._sshTab(),
              if (_error.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: Text(
                    _error,
                    style: const TextStyle(color: inkDanger, fontSize: 11),
                  ),
                ),
              const Padding(
                padding: EdgeInsets.only(top: 10, bottom: 8),
                child: Divider(height: 1),
              ),
              const _SectionTitle('帮助与更新'),
              Row(
                children: <Widget>[
                  TextButton(
                    onPressed: () => showUsageGuide(context),
                    child: const Text('使用说明'),
                  ),
                  TextButton(
                    onPressed: _checkingUpdate ? null : _checkUpdate,
                    child: Text(_checkingUpdate ? '正在检查…' : '检查更新'),
                  ),
                  TextButton(
                    onPressed: widget.onOpenRepository,
                    child: const Text('GitHub 仓库'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(onPressed: _save, child: const Text('保存')),
      ],
    );
  }

  Widget _tabBar() {
    const List<String> labels = <String>['Tailscale', '一次性短码', 'SSH 中继'];
    return Row(
      children: <Widget>[
        for (int index = 0; index < labels.length; index++)
          Expanded(
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: () => setState(() => _tab = index),
              child: Container(
                padding: const EdgeInsets.symmetric(vertical: 9),
                decoration: BoxDecoration(
                  border: Border(
                    bottom: BorderSide(
                      color: _tab == index ? inkTeal : inkBorder,
                      width: _tab == index ? 2.0 : 1.0,
                    ),
                  ),
                ),
                child: Text(
                  labels[index],
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: 11,
                    color: _tab == index ? inkTeal : inkTextSecondary,
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }

  List<Widget> _tailscaleTab() {
    return <Widget>[
      const Text(
        '填对方的 Tailscale IP 或 MagicDNS 名称，保存后会定向探测该地址',
        style: TextStyle(color: inkTextDim, fontSize: 11),
      ),
      const SizedBox(height: 6),
      TextField(
        key: const Key('manual-peer-name'),
        controller: _manualName,
        decoration: const InputDecoration(labelText: '设备备注（可选）'),
      ),
      const SizedBox(height: 6),
      TextField(
        key: const Key('manual-peer-host'),
        controller: _manualHost,
        decoration: const InputDecoration(labelText: 'Tailscale IP 或 MagicDNS'),
      ),
      const SizedBox(height: 6),
      TextField(
        key: const Key('manual-peer-port'),
        controller: _manualPort,
        keyboardType: TextInputType.number,
        decoration: const InputDecoration(labelText: '对方发现 UDP 端口（留空=默认）'),
      ),
      if (_manualError.isNotEmpty)
        Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(
            _manualError,
            style: const TextStyle(color: inkDanger, fontSize: 11),
          ),
        ),
      Align(
        alignment: Alignment.centerLeft,
        child: TextButton(
          onPressed: () => _submitManualPeer(),
          child: Text(_editing < 0 ? '添加设备' : '保存设备'),
        ),
      ),
      for (int index = 0; index < _manualPeers.length; index++)
        Row(
          children: <Widget>[
            Expanded(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  if (_manualPeers[index].name.isNotEmpty)
                    Text(
                      _manualPeers[index].name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: inkTextPrimary,
                        fontSize: 13,
                      ),
                    ),
                  Text(
                    _manualPeers[index].port == 0
                        ? _manualPeers[index].host
                        : '${_manualPeers[index].host}:${_manualPeers[index].port}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: _hintStyle,
                  ),
                ],
              ),
            ),
            TextButton(
              onPressed: () {
                final ManualPeer peer = _manualPeers[index];
                setState(() {
                  _manualName.text = peer.name;
                  _manualHost.text = peer.host;
                  _manualPort.text = peer.port == 0 ? '' : peer.port.toString();
                  _editing = index;
                });
              },
              child: const Text('编辑'),
            ),
            TextButton(
              onPressed: () => setState(() {
                _manualPeers.removeAt(index);
                _editing = -1;
              }),
              child: const Text('删除'),
            ),
          ],
        ),
    ];
  }

  List<Widget> _wormholeTab() {
    return <Widget>[
      const Text(
        '这里只配置服务地址；一次性发送和输入短码接收请在主页操作',
        style: TextStyle(color: inkTextDim, fontSize: 11),
      ),
      const SizedBox(height: 6),
      TextField(
        controller: _rendezvous,
        decoration: const InputDecoration(labelText: '配对服务地址（留空=默认）'),
      ),
      const SizedBox(height: 7),
      TextField(
        controller: _transit,
        decoration: const InputDecoration(labelText: '传输中继地址（留空=默认）'),
      ),
    ];
  }

  List<Widget> _sshTab() {
    return <Widget>[
      Row(
        children: <Widget>[
          const Expanded(
            child: Text(
              '启用 SSH VPS 中继',
              style: TextStyle(color: inkTextPrimary, fontSize: 13),
            ),
          ),
          Switch(
            value: _sshEnabled,
            onChanged: (bool value) => setState(() => _sshEnabled = value),
          ),
        ],
      ),
      TextField(
        controller: _sshHost,
        decoration: const InputDecoration(labelText: 'VPS 公网 IP 或域名'),
      ),
      const SizedBox(height: 6),
      Row(
        children: <Widget>[
          Expanded(
            flex: 4,
            child: TextField(
              controller: _sshPort,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(labelText: 'SSH 端口'),
            ),
          ),
          const SizedBox(width: 7),
          Expanded(
            flex: 5,
            child: TextField(
              controller: _sshUser,
              decoration: const InputDecoration(labelText: 'SSH 用户'),
            ),
          ),
        ],
      ),
      const SizedBox(height: 7),
      TextField(
        controller: _sshKey,
        autocorrect: false,
        obscureText: true,
        decoration: InputDecoration(
          labelText: _keyStored ? 'SSH 私钥（已安全保存）' : '粘贴 OpenSSH / PEM 私钥',
        ),
      ),
      const SizedBox(height: 7),
      TextField(
        controller: _sshPassphrase,
        obscureText: true,
        decoration: InputDecoration(
          labelText: _passphraseStored ? '私钥口令（已安全保存）' : '私钥口令（可选）',
        ),
      ),
      const SizedBox(height: 6),
      SelectionArea(
        child: Text(
          '主机指纹：${_fingerprint.isEmpty ? '尚未验证' : _fingerprint}',
          style: _hintStyle,
        ),
      ),
      Align(
        alignment: Alignment.centerLeft,
        child: TextButton(
          onPressed: _checking ? null : _verifySsh,
          child: Text(_checking ? '验证中…' : '验证连接与指纹'),
        ),
      ),
      Row(
        children: <Widget>[
          TextButton(
            onPressed: widget.sshReady ? widget.onCreateSshPair : null,
            child: const Text('生成配对码'),
          ),
          TextButton(
            onPressed: () {
              Navigator.of(context).pop();
              widget.onOpenCrossNetwork();
            },
            child: const Text('输入配对码'),
          ),
        ],
      ),
    ];
  }
}

const TextStyle _hintStyle = TextStyle(color: inkTextSecondary, fontSize: 12);

class _SectionTitle extends StatelessWidget {
  const _SectionTitle(this.title);

  final String title;

  @override
  Widget build(BuildContext context) {
    return Text(
      title,
      style: const TextStyle(
        color: inkTextPrimary,
        fontSize: 14,
        fontWeight: FontWeight.w600,
      ),
    );
  }
}
