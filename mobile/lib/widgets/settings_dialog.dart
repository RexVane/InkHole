import 'package:flutter/material.dart';

import '../models.dart';
import '../theme.dart';
import 'cross_network_dialog.dart';
import 'usage_guide_dialog.dart';

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
    required this.sshReady,
    required this.onCheckSsh,
    required this.onCreateSshPair,
    required this.onOpenCrossNetwork,
  });

  final InkSettings initial;
  final String deviceLine;
  final String portLine;
  final String inboxPath;
  final bool sshReady;

  /// 返回 ssh.check 的原始结果；失败时返回 null，错误由页面用状态栏提示。
  final Future<Map<String, dynamic>?> Function(InkSettings draft) onCheckSsh;

  final VoidCallback onCreateSshPair;
  final VoidCallback onOpenCrossNetwork;

  @override
  State<SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<SettingsDialog> {
  late final TextEditingController _name;
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

  late bool _encryption;
  late bool _sshEnabled;
  late String _fingerprint;
  late List<ManualPeer> _manualPeers;
  int _tab = 0;
  int _editing = -1;
  bool _checking = false;
  String _manualError = '';
  String _error = '';

  @override
  void initState() {
    super.initState();
    final InkSettings initial = widget.initial;
    _name = TextEditingController(text: initial.peerName);
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
    _manualPeers = List<ManualPeer>.of(initial.manualPeers);
  }

  @override
  void dispose() {
    _name.dispose();
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
    super.dispose();
  }

  InkSettings _draft() {
    final String name = _name.text.trim();
    return InkSettings(
      peerName: name.length > 40 ? name.substring(0, 40) : name,
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

  void _submitManualPeer() {
    final String host = _manualHost.text.trim();
    if (host.isEmpty || host.contains(' ')) {
      setState(() => _manualError = 'Tailscale IP 或 MagicDNS 名称无效');
      return;
    }
    final ManualPeer peer = ManualPeer(name: _manualName.text.trim(), host: host);
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
      _manualHost.clear();
    });
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
    final int? sshPort = int.tryParse(_sshPort.text.trim());
    if (_sshEnabled && (sshPort == null || sshPort < 1 || sshPort > 65535)) {
      setState(() => _error = 'SSH 端口必须在 1-65535 范围内');
      return;
    }
    if (_name.text.trim().isEmpty) {
      setState(() => _error = '设备名称不能为空');
      return;
    }
    Navigator.of(context).pop(_draft());
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: inkBgCard,
      title: const Text('设置', style: TextStyle(color: inkTextPrimary)),
      content: SingleChildScrollView(
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
            const SizedBox(height: 14),
            const _SectionTitle('存储'),
            const SizedBox(height: 4),
            const Text(
              '默认目录',
              style: TextStyle(color: inkTextPrimary, fontSize: 13),
            ),
            Text(widget.inboxPath, style: _hintStyle),
            const Text(
              '所有接收文件和文件夹统一保存在这里',
              style: TextStyle(color: inkTextDim, fontSize: 11),
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
            const Divider(height: 26),
            const _SectionTitle('帮助'),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton(
                onPressed: () => showUsageGuide(context),
                child: const Text('使用说明'),
              ),
            ),
          ],
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
        controller: _manualName,
        decoration: const InputDecoration(labelText: '设备备注（可选）'),
      ),
      const SizedBox(height: 6),
      TextField(
        controller: _manualHost,
        decoration: const InputDecoration(labelText: 'Tailscale IP 或 MagicDNS'),
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
          onPressed: _submitManualPeer,
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
                    _manualPeers[index].host,
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
            child: TextField(
              controller: _sshPort,
              keyboardType: TextInputType.number,
              decoration: const InputDecoration(labelText: 'SSH 端口'),
            ),
          ),
          const SizedBox(width: 7),
          Expanded(
            flex: 2,
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
        decoration: const InputDecoration(labelText: '粘贴 OpenSSH / PEM 私钥'),
      ),
      const SizedBox(height: 7),
      TextField(
        controller: _sshPassphrase,
        obscureText: true,
        decoration: const InputDecoration(labelText: '私钥口令（可选）'),
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
