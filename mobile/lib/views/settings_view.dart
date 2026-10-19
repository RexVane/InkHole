import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/updater.dart';
import '../models.dart';
import '../theme.dart';
import '../widgets/iconsax.dart';

/// Tab 4: 设置视图 (Settings View)
class SettingsView extends StatefulWidget {
  const SettingsView({
    super.key,
    required this.initialSettings,
    required this.instanceId,
    required this.actualPort,
    required this.inboxPath,
    this.appInboxPath = '',
    required this.onBack,
    required this.onSave,
    required this.onChooseDirectory,
    required this.onResetDirectory,
    required this.onOpenSshRelay,
  });

  final InkSettings initialSettings;
  final String instanceId;
  final int actualPort;
  final String inboxPath;
  final String appInboxPath;
  final VoidCallback onBack;
  final Future<void> Function(InkSettings settings) onSave;
  final VoidCallback onChooseDirectory;
  final VoidCallback onResetDirectory;

  /// 打开 SSH 中继控制台（连接参数、指纹确认、配对都在那里）。
  final VoidCallback onOpenSshRelay;

  @override
  State<SettingsView> createState() => _SettingsViewState();
}

class _SettingsViewState extends State<SettingsView> {
  late final TextEditingController _nameController;
  late final TextEditingController _portController;
  late final TextEditingController _rendezvousController;
  late final TextEditingController _relayController;
  late final TextEditingController _secretController;
  final TextEditingController _manualNameController = TextEditingController();
  final TextEditingController _manualHostController = TextEditingController();
  final TextEditingController _manualPortController = TextEditingController();

  late List<ManualPeer> _manualPeers;
  int _editingPeerIndex = -1;
  String _manualError = '';

  bool _isSaving = false;
  bool _checkingUpdate = false;
  late bool _encryptionEnabled;
  late bool _sshEnabled;

  static const String _releasesUrl = '$repositoryUrl/releases/latest';

  @override
  void initState() {
    super.initState();
    _nameController = TextEditingController(text: widget.initialSettings.peerName);
    _portController = TextEditingController(text: _portText(widget.initialSettings));
    _rendezvousController = TextEditingController(
      text: normalizeRendezvousUrl(widget.initialSettings.rendezvousUrl),
    );
    _relayController = TextEditingController(text: widget.initialSettings.transitRelay);
    _secretController = TextEditingController(text: widget.initialSettings.secret);
    _manualPeers = List<ManualPeer>.of(widget.initialSettings.manualPeers);
    _encryptionEnabled = widget.initialSettings.encryptionEnabled;
    _sshEnabled = widget.initialSettings.sshEnabled;
  }

  @override
  void didUpdateWidget(covariant SettingsView oldWidget) {
    super.didUpdateWidget(oldWidget);
    // 输入框还停在上一份设置上时才跟着刷新，避免盖掉用户正在改的内容。
    _syncIfUntouched(
      _nameController,
      oldWidget.initialSettings.peerName,
      widget.initialSettings.peerName,
    );
    _syncIfUntouched(
      _portController,
      _portText(oldWidget.initialSettings),
      _portText(widget.initialSettings),
    );
    _syncIfUntouched(
      _rendezvousController,
      normalizeRendezvousUrl(oldWidget.initialSettings.rendezvousUrl),
      normalizeRendezvousUrl(widget.initialSettings.rendezvousUrl),
    );
    _syncIfUntouched(
      _relayController,
      oldWidget.initialSettings.transitRelay,
      widget.initialSettings.transitRelay,
    );
    if (_editingPeerIndex < 0 &&
        _manualHostController.text.trim().isEmpty &&
        oldWidget.initialSettings.manualPeers != widget.initialSettings.manualPeers) {
      _manualPeers = List<ManualPeer>.of(widget.initialSettings.manualPeers);
    }
  }

  void _syncIfUntouched(TextEditingController controller, String previous, String next) {
    if (controller.text != previous || previous == next) return;
    controller.value = TextEditingValue(
      text: next,
      selection: TextSelection.collapsed(offset: next.length),
    );
  }

  String _portText(InkSettings settings) =>
      settings.listenPort > 0 ? settings.listenPort.toString() : '';

  @override
  void dispose() {
    _nameController.dispose();
    _portController.dispose();
    _rendezvousController.dispose();
    _relayController.dispose();
    _secretController.dispose();
    _manualNameController.dispose();
    _manualHostController.dispose();
    _manualPortController.dispose();
    super.dispose();
  }

  /// 提交正在编辑的 Tailscale / 手动发现节点；输入非法返回 false 并留下提示。
  bool _submitManualPeer() {
    final String rawHost = _manualHostController.text.trim();
    if (rawHost.isEmpty || rawHost.contains(' ')) {
      setState(() => _manualError = 'Tailscale IP 或 MagicDNS 名称无效');
      return false;
    }
    final String portText = _manualPortController.text.trim();
    final int manualPort =
        portText.isEmpty ? 0 : (int.tryParse(portText) ?? -1);
    if (manualPort < 0 || manualPort > 65535) {
      setState(() => _manualError = '对方发现 UDP 端口需为 1-65535 或留空');
      return false;
    }
    final ManualPeer parsed = parseManualEndpoint(
      manualPort > 0
          ? (rawHost.contains(':') && !rawHost.startsWith('[')
              ? '[$rawHost]:$manualPort'
              : '$rawHost:$manualPort')
          : rawHost,
      name: _manualNameController.text.trim(),
    );
    if (parsed.host.isEmpty) {
      setState(() => _manualError = 'Tailscale IP 或 MagicDNS 名称无效');
      return false;
    }
    final ManualPeer peer = ManualPeer(
      name: _manualNameController.text.trim(),
      host: parsed.host,
      port: manualPort > 0 ? manualPort : parsed.port,
    );
    setState(() {
      _manualError = '';
      if (_editingPeerIndex >= 0 && _editingPeerIndex < _manualPeers.length) {
        _manualPeers[_editingPeerIndex] = peer;
      } else {
        _manualPeers.removeWhere((ManualPeer item) => item.host == peer.host);
        _manualPeers.add(peer);
      }
      _editingPeerIndex = -1;
      _manualNameController.clear();
      _manualHostController.clear();
      _manualPortController.clear();
    });
    return true;
  }

  bool _commitPendingManualPeer() {
    if (_manualHostController.text.trim().isEmpty) return true;
    return _submitManualPeer();
  }

  Future<void> _handleSave() async {
    if (_isSaving) return;

    final int port = int.tryParse(_portController.text.trim()) ?? 0;
    if (port != 0 && (port < 1 || port > 65535)) {
      await _showNotice('端口无效', '监听端口需在 1-65535 之间，留空表示自动选择。');
      return;
    }
    final String rendezvous = normalizeRendezvousUrl(_rendezvousController.text);
    if (rendezvous.isNotEmpty &&
        !rendezvous.startsWith('ws://') &&
        !rendezvous.startsWith('wss://')) {
      await _showNotice(
        '配对服务地址无效',
        '需以 ws:// 或 wss:// 开头，例如 $defaultRendezvousUrl。',
      );
      return;
    }
    final String relay = _relayController.text.trim();
    if (relay.isNotEmpty && !_isValidHostPort(relay)) {
      await _showNotice('中继地址无效', '格式应为 host:port，例如 transit.magic-wormhole.io:4001。');
      return;
    }
    if (!_commitPendingManualPeer()) {
      return;
    }

    setState(() => _isSaving = true);
    final List<ManualPeer> manualPeers = List<ManualPeer>.of(_manualPeers);

    final InkSettings updated = InkSettings(
      peerName: _nameController.text.trim().isNotEmpty
          ? _nameController.text.trim()
          : 'InkHole',
      listenPort: port,
      encryptionEnabled: _encryptionEnabled,
      secret: _secretController.text.trim().isNotEmpty
          ? _secretController.text.trim()
          : widget.initialSettings.secret,
      manualPeers: manualPeers,
      rendezvousUrl: rendezvous,
      transitRelay: relay,
      sshEnabled: _sshEnabled,
      // SSH 连接参数一律由中继控制台维护（那里才有「测试握手」与指纹确认），
      // 本页只透传 home_page 当前持有的值，避免用一个从未被编辑过的输入框
      // 把控制台刚存下的配置覆盖回旧值。
      sshHost: widget.initialSettings.sshHost,
      sshPort: widget.initialSettings.sshPort,
      sshUser: widget.initialSettings.sshUser,
      sshFingerprint: widget.initialSettings.sshFingerprint,
      sshPrivateKey: widget.initialSettings.sshPrivateKey,
      sshPassphrase: widget.initialSettings.sshPassphrase,
      sshRemotePort: widget.initialSettings.sshRemotePort,
    );

    try {
      await widget.onSave(updated);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('配置已保存并生效'),
          backgroundColor: surfaceActive,
          duration: Duration(seconds: 2),
        ),
      );
    } catch (error) {
      // 保存失败必须复位按钮，否则 _isSaving 永久为 true，保存键彻底不可用。
      if (!mounted) return;
      await _showNotice('保存失败', '$error');
    } finally {
      if (mounted) setState(() => _isSaving = false);
    }
  }

  /// 校验 host:port 形式。IPv6 需写成 `[::1]:4001`。
  static bool _isValidHostPort(String value) {
    final Uri? uri = Uri.tryParse('tcp://$value');
    if (uri == null) return false;
    if (uri.host.isEmpty) return false;
    // 传了端口就必须落在合法范围(Uri.port 对越界/非法端口返回 0)。
    final bool hasPortSyntax = value.contains(':') &&
        !(value.startsWith('[') && value.endsWith(']'));
    if (hasPortSyntax && uri.port == 0) return false;
    return true;
  }

  Future<UpdateInfo> _loadUpdate() async {
    if (Platform.isAndroid) {
      try {
        return _ensureDownloadUrl(await UpdaterChannel.check(appVersion));
      } on MissingPluginException {
        return ReleaseDownloader().fetch(appVersion);
      } on PlatformException {
        return ReleaseDownloader().fetch(appVersion);
      }
    }
    return ReleaseDownloader().fetch(appVersion);
  }

  UpdateInfo _ensureDownloadUrl(UpdateInfo info) {
    if (!info.newer || info.apkUrl.trim().isNotEmpty) return info;
    // 原生刻意返回空 URL 表示"没有适配本机的安装包"(例如 Release 里没有
    // 对应 ABI)。此时必须保留空 URL，让 UI 走"前往发布页"分支，
    // 而不是猜一个地址让用户下载到跑不起来的包。
    if (info.fileName.isNotEmpty) return info;
    final String name = Platform.isIOS
        ? 'InkHole-${info.version}-ios.zip'
        : 'InkHole-${info.version}.apk';
    return UpdateInfo(
      version: info.version,
      apkUrl: releaseAssetUrl(info.version, name),
      notes: info.notes,
      newer: true,
      fileName: name,
    );
  }

  Future<void> _checkUpdate() async {
    setState(() => _checkingUpdate = true);
    final UpdateInfo info;
    try {
      info = await _loadUpdate();
    } catch (error) {
      if (!mounted) return;
      setState(() => _checkingUpdate = false);
      await _showNotice('检查更新失败', _updateError(error));
      return;
    }
    if (!mounted) return;
    setState(() => _checkingUpdate = false);
    if (!info.newer) {
      await _showNotice('检查更新', '当前已是最新版本 v$appVersion');
      return;
    }

    final bool canInstall = info.apkUrl.isNotEmpty;
    final String notes =
        info.notes.trim().isEmpty ? '新版本已发布，是否立即更新？' : info.notes.trim();
    final bool? confirmed = await showDialog<bool>(
      context: context,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: Text(
            '发现新版本 ${info.version}',
            style: const TextStyle(
              color: textPrimary,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
          content: ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 240),
            child: SingleChildScrollView(
              child: Text(
                canInstall ? notes : '$notes\n\n没有找到适合本机的安装包。\n$_releasesUrl',
                style: const TextStyle(color: textMuted, fontSize: 13, height: 1.45),
              ),
            ),
          ),
          actions: <Widget>[
            if (canInstall)
              TextButton(
                onPressed: () => Navigator.of(dialogContext).pop(false),
                child: const Text('稍后', style: TextStyle(color: textMuted)),
              ),
            ElevatedButton(
              onPressed: () => Navigator.of(dialogContext).pop(canInstall),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                foregroundColor: bgAbyss,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: Text(
                canInstall ? '立即更新' : '好',
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
            ),
          ],
        );
      },
    );
    if (confirmed == true && mounted) {
      try {
        await _downloadUpdate(info);
      } catch (error) {
        if (mounted) await _showNotice('下载更新失败', _updateError(error));
      }
    }
  }

  Future<void> _downloadUpdate(UpdateInfo info) async {
    if (Platform.isAndroid) {
      try {
        await _downloadAndInstall(info.apkUrl);
        return;
      } on MissingPluginException {
        // 桌面调试没有安装器时，仍然把安装包下载到应用目录。
      }
    }
    final File saved = await _downloadInsideApp(info);
    if (!mounted) return;
    await _showNotice(
      '已下载到应用内',
      '文件已保存：\n${saved.path}',
    );
  }

  Future<File> _downloadInsideApp(UpdateInfo info) async {
    final ValueNotifier<int> progress = ValueNotifier<int>(0);
    var dialogOpen = true;
    unawaited(
      _showProgressDialog(progress).whenComplete(() => dialogOpen = false),
    );
    try {
      return await ReleaseDownloader().download(info, (int percent) {
        progress.value = percent;
      });
    } finally {
      if (mounted && dialogOpen) {
        Navigator.of(context, rootNavigator: true).pop();
      }
      progress.dispose();
    }
  }

  Future<void> _showProgressDialog(ValueNotifier<int> progress) {
    return showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Text(
            '正在下载更新',
            style: TextStyle(
              color: textPrimary,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
          content: ValueListenableBuilder<int>(
            valueListenable: progress,
            builder: (BuildContext context, int percent, Widget? child) {
              return Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  ClipRRect(
                    borderRadius: BorderRadius.circular(4),
                    child: LinearProgressIndicator(
                      value: percent <= 0 ? null : percent / 100,
                      minHeight: 6,
                      backgroundColor: surfaceHigh,
                      color: jade400,
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text(
                    percent <= 0 ? '正在连接 GitHub…' : '$percent%',
                    style: const TextStyle(
                      color: textMuted,
                      fontSize: 12,
                      fontFamily: 'monospace',
                    ),
                  ),
                ],
              );
            },
          ),
        );
      },
    );
  }

  Future<void> _downloadAndInstall(String url) async {
    final ValueNotifier<int> progress = ValueNotifier<int>(0);
    UpdaterChannel.onProgress = (int percent) => progress.value = percent;
    var dialogOpen = true;
    unawaited(
      _showProgressDialog(progress).whenComplete(() => dialogOpen = false),
    );
    Object? failure;
    try {
      await UpdaterChannel.downloadInstall(url);
    } catch (error) {
      failure = error;
    } finally {
      UpdaterChannel.onProgress = null;
    }
    if (mounted && dialogOpen) {
      Navigator.of(context, rootNavigator: true).pop();
    }
    progress.dispose();
    if (failure is MissingPluginException) throw failure;
    if (failure != null && mounted) {
      await _showNotice('下载更新失败', _updateError(failure));
    }
  }

  Future<void> _showNotice(String title, String message) async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: Text(
            title,
            style: const TextStyle(
              color: textPrimary,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
          content: SelectableText(
            message,
            style: const TextStyle(color: textMuted, fontSize: 13, height: 1.45),
          ),
          actions: <Widget>[
            ElevatedButton(
              onPressed: () => Navigator.of(dialogContext).pop(),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                foregroundColor: bgAbyss,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text('好', style: TextStyle(fontWeight: FontWeight.w700)),
            ),
          ],
        );
      },
    );
  }

  String _updateError(Object error) {
    if (error is PlatformException) {
      final String message = (error.message ?? '').trim();
      return message.isEmpty ? error.code : message;
    }
    return '$error';
  }

  @override
  Widget build(BuildContext context) {
    final String fullInstanceId =
        widget.instanceId.isEmpty ? '生成中' : widget.instanceId;

    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        leading: IconButton(
          icon: const IconsaxIcon(
            Iconsax.arrowLeft,
            size: 20,
            color: textMuted,
          ),
          onPressed: widget.onBack,
        ),
        title: const Text(
          '设置',
          style: TextStyle(
            color: textPrimary,
            fontSize: 16,
            fontWeight: FontWeight.w700,
          ),
        ),
        actions: const <Widget>[],
      ),
      body: SingleChildScrollView(
        physics: const BouncingScrollPhysics(),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            // 顶部系统元数据卡片
            _buildTelemetryHeroCard(fullInstanceId),
            const SizedBox(height: 14),

            // 分区 1：设备设置
            _buildDeviceSection(),
            const SizedBox(height: 14),

            // 分区 2：存储设置
            _buildStorageSection(),
            const SizedBox(height: 14),

            // 分区 3：高级中继与网络
            _buildNetworkSection(),
            const SizedBox(height: 14),

            // 分区 4：版本与升级
            _buildUpdateSection(),
            const SizedBox(height: 18),

            // 底部操作栏 (返回 / 保存)
            _buildBottomActionBar(),
            const SizedBox(height: 28),
          ],
        ),
      ),
    );
  }

  Widget _buildTelemetryHeroCard(String fullInstanceId) {
    final String deviceName = widget.initialSettings.peerName.trim().isNotEmpty
        ? widget.initialSettings.peerName.trim()
        : 'InkHole';

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: borderLuminescent),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Row(
                    children: <Widget>[
                      Container(
                        width: 6,
                        height: 6,
                        decoration: const BoxDecoration(
                          color: jade400,
                          shape: BoxShape.circle,
                        ),
                      ),
                      const SizedBox(width: 8),
                      const Text(
                        '本机节点信息',
                        style: TextStyle(
                          color: textPrimary,
                          fontSize: 16,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 3),
                  const Text(
                    '本机名称、标识、监听端口与应用版本',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                ],
              ),
              const IconsaxBox(
                icon: Iconsax.tune,
                size: 38,
                iconSize: 19,
                borderRadius: 11,
                color: jade400,
              ),
            ],
          ),
          const SizedBox(height: 12),

          // 元数据四行条目
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: surfaceBorder),
            ),
            child: Column(
              children: <Widget>[
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    const Row(
                      children: <Widget>[
                        IconsaxIcon(
                          Iconsax.devicePhone,
                          color: textMuted,
                          size: 14,
                        ),
                        SizedBox(width: 6),
                        Text(
                          '本机设备',
                          style: TextStyle(color: textDim, fontSize: 11),
                        ),
                      ],
                    ),
                    Flexible(
                      child: Text(
                        deviceName,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: jade400,
                          fontSize: 11,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                InkWell(
                  onTap: widget.instanceId.isEmpty
                      ? null
                      : () {
                          Clipboard.setData(
                            ClipboardData(text: widget.instanceId),
                          );
                          ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(
                              content: Text('已复制本机标识'),
                              backgroundColor: surfaceActive,
                              duration: Duration(seconds: 2),
                            ),
                          );
                        },
                  borderRadius: BorderRadius.circular(6),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: <Widget>[
                      const Row(
                        children: <Widget>[
                          IconsaxIcon(
                            Iconsax.fingerprint,
                            color: textMuted,
                            size: 14,
                          ),
                          SizedBox(width: 6),
                          Text(
                            '本机标识',
                            style: TextStyle(color: textDim, fontSize: 11),
                          ),
                        ],
                      ),
                      const SizedBox(width: 8),
                      Flexible(
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: <Widget>[
                            Flexible(
                              child: Text(
                                fullInstanceId,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: textPrimary,
                                  fontSize: 11,
                                  fontFamily: 'monospace',
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                            if (widget.instanceId.isNotEmpty) ...<Widget>[
                              const SizedBox(width: 4),
                              const IconsaxIcon(
                                Iconsax.copy,
                                size: 12,
                                color: jade400,
                              ),
                            ],
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 6),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    const Row(
                      children: <Widget>[
                        IconsaxIcon(
                          Iconsax.layers,
                          color: textMuted,
                          size: 14,
                        ),
                        SizedBox(width: 6),
                        Text(
                          '应用版本',
                          style: TextStyle(color: textDim, fontSize: 11),
                        ),
                      ],
                    ),
                    Text(
                      'v$appVersion',
                      style: const TextStyle(
                        color: badgeRoute,
                        fontSize: 11,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    const Row(
                      children: <Widget>[
                        IconsaxIcon(Iconsax.hub, color: textMuted, size: 14),
                        SizedBox(width: 6),
                        Text(
                          '监听状态',
                          style: TextStyle(color: textDim, fontSize: 11),
                        ),
                      ],
                    ),
                    Text(
                      widget.initialSettings.listenPort == 0
                          ? '${widget.actualPort}（本次临时）'
                          : '${widget.actualPort}（固定）',
                      style: const TextStyle(
                        color: textMuted,
                        fontSize: 11,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 4),
          const Align(
            alignment: Alignment.centerRight,
            child: Text(
              '点按本机标识可复制完整 ID · 建议自定义 1024–49151 固定端口',
              style: TextStyle(color: textDim, fontSize: 10),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildDeviceSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const Row(
          children: <Widget>[
            IconsaxIcon(Iconsax.server, color: jade400, size: 15),
            SizedBox(width: 6),
            Text(
              '设备设置',
              style: TextStyle(
                color: textPrimary,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),

        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: surfaceContainer,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: surfaceBorder),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              const Text(
                '设备名称',
                style: TextStyle(color: textMuted, fontSize: 11),
              ),
              const SizedBox(height: 6),
              TextField(
                controller: _nameController,
                style: const TextStyle(color: textPrimary, fontSize: 13),
                decoration: const InputDecoration(
                  hintText: '输入设备名称...',
                  suffixIcon: Padding(
                    padding: EdgeInsets.all(12),
                    child: IconsaxIcon(
                      Iconsax.edit,
                      size: 16,
                      color: jade400,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 12),

              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: <Widget>[
                  const Text(
                    '本机监听端口 (默认 41300，留空=随机)',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 6,
                      vertical: 2,
                    ),
                    decoration: BoxDecoration(
                      color: surfaceHigh,
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: const Text(
                      'QUIC',
                      style: TextStyle(
                        color: badgeRoute,
                        fontSize: 9,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              TextField(
                controller: _portController,
                keyboardType: TextInputType.number,
                style: const TextStyle(
                  color: jade400,
                  fontSize: 13,
                  fontFamily: 'monospace',
                  fontWeight: FontWeight.w700,
                ),
                decoration: const InputDecoration(
                  hintText: '41300',
                  suffixIcon: Padding(
                    padding: EdgeInsets.all(12),
                    child: IconsaxIcon(
                      Iconsax.unlock,
                      size: 16,
                      color: textDim,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 6),
              const Text(
                '固定端口便于防火墙放行与 Tailscale 直连；端口被占用时会自动改用随机端口。',
                style: TextStyle(color: textDim, fontSize: 10, height: 1.3),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildStorageSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const Row(
          children: <Widget>[
            IconsaxIcon(Iconsax.folder, color: jade400, size: 15),
            SizedBox(width: 6),
            Text(
              '存储',
              style: TextStyle(
                color: textPrimary,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),

        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: surfaceContainer,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: surfaceBorder),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              const Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: <Widget>[
                  Text(
                    '收件目录',
                    style: TextStyle(
                      color: textPrimary,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  SizedBox.shrink(),
                ],
              ),
              const SizedBox(height: 8),

              // 路径卡片
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: surfaceLowest,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: surfaceBorder),
                ),
                child: Row(
                  children: <Widget>[
                    const IconsaxIcon(
                      Iconsax.folder,
                      color: jade400,
                      size: 16,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        widget.inboxPath.isNotEmpty
                            ? widget.inboxPath
                            : '应用内收件箱',
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: textPrimary,
                          fontSize: 11,
                          fontFamily: 'monospace',
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              if (widget.appInboxPath.isNotEmpty) ...<Widget>[
                const SizedBox(height: 6),
                Text(
                  '导出失败时暂存于应用内：${widget.appInboxPath}',
                  style: const TextStyle(
                    color: textDim,
                    fontSize: 10,
                    fontFamily: 'monospace',
                    height: 1.3,
                  ),
                ),
              ],
              const SizedBox(height: 8),
              const Text(
                '接收的文件先放在应用内收件箱。从收件箱导出时，Android 默认进系统下载目录。',
                style: TextStyle(color: textDim, fontSize: 10, height: 1.3),
              ),
              const SizedBox(height: 10),

              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: <Widget>[
                  TextButton.icon(
                    onPressed: widget.onResetDirectory,
                    icon: const IconsaxIcon(
                      Iconsax.refresh,
                      size: 14,
                      color: textMuted,
                    ),
                    label: const Text(
                      '恢复默认',
                      style: TextStyle(color: textMuted, fontSize: 11),
                    ),
                  ),
                  const SizedBox(width: 8),
                  ElevatedButton.icon(
                    onPressed: widget.onChooseDirectory,
                    icon: const IconsaxIcon(
                      Iconsax.folder,
                      size: 14,
                      color: jade400,
                    ),
                    label: const Text(
                      '选择目录',
                      style: TextStyle(color: jade400, fontSize: 11),
                    ),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: surfaceHigh,
                      elevation: 0,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(10),
                        side: const BorderSide(color: borderLuminescent),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildNetworkSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const Row(
          children: <Widget>[
            IconsaxIcon(Iconsax.route, color: jade400, size: 15),
            SizedBox(width: 6),
            Text(
              '高级中继与网络',
              style: TextStyle(
                color: textPrimary,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),

        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: surfaceContainer,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: surfaceBorder),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              // Wormhole 配对服务地址 + 传输中继服务器
              const Text(
                'Wormhole 配对服务地址（留空=默认）',
                style: TextStyle(color: textMuted, fontSize: 11),
              ),
              const SizedBox(height: 6),
              TextField(
                controller: _rendezvousController,
                style: const TextStyle(
                  color: textPrimary,
                  fontSize: 12,
                  fontFamily: 'monospace',
                ),
                decoration: const InputDecoration(
                  hintText: defaultRendezvousUrl,
                  suffixIcon: Padding(
                    padding: EdgeInsets.all(12),
                    child: IconsaxIcon(
                      Iconsax.link,
                      size: 16,
                      color: textDim,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 10),
              const Text(
                'Wormhole 传输中继服务器（留空=默认）',
                style: TextStyle(color: textMuted, fontSize: 11),
              ),
              const SizedBox(height: 6),
              TextField(
                controller: _relayController,
                style: const TextStyle(
                  color: textPrimary,
                  fontSize: 12,
                  fontFamily: 'monospace',
                ),
                decoration: const InputDecoration(
                  hintText: 'transit.magic-wormhole.io:4001',
                  suffixIcon: Padding(
                    padding: EdgeInsets.all(12),
                    child: IconsaxIcon(
                      Iconsax.cloud,
                      size: 16,
                      color: textDim,
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                '支持自建 VPS 配对与传输中继节点，留空则使用官方公共服务。',
                style: TextStyle(color: textDim, fontSize: 10),
              ),
              const SizedBox(height: 14),

              // Tailscale 固定设备列表
              const Text(
                'Tailscale / 固定探测节点',
                style: TextStyle(
                  color: textPrimary,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                '填对方的 Tailscale IP 或 MagicDNS 名称，保存后会定向探测该地址。',
                style: TextStyle(color: textDim, fontSize: 10),
              ),
              const SizedBox(height: 8),
              TextField(
                key: const Key('manual-peer-name'),
                controller: _manualNameController,
                style: const TextStyle(color: textPrimary, fontSize: 12),
                decoration: const InputDecoration(
                  labelText: '设备备注（可选）',
                  isDense: true,
                ),
              ),
              const SizedBox(height: 8),
              Row(
                children: <Widget>[
                  Expanded(
                    flex: 5,
                    child: TextField(
                      key: const Key('manual-peer-host'),
                      controller: _manualHostController,
                      style: const TextStyle(
                        color: textPrimary,
                        fontSize: 12,
                        fontFamily: 'monospace',
                      ),
                      decoration: const InputDecoration(
                        labelText: 'Tailscale IP 或 MagicDNS',
                        isDense: true,
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    flex: 3,
                    child: TextField(
                      key: const Key('manual-peer-port'),
                      controller: _manualPortController,
                      keyboardType: TextInputType.number,
                      style: const TextStyle(
                        color: textPrimary,
                        fontSize: 12,
                        fontFamily: 'monospace',
                      ),
                      decoration: const InputDecoration(
                        labelText: '发现端口(可选)',
                        isDense: true,
                      ),
                    ),
                  ),
                ],
              ),
              if (_manualError.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    _manualError,
                    style: const TextStyle(color: dangerCoral, fontSize: 11),
                  ),
                ),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: _submitManualPeer,
                  icon: IconsaxIcon(
                    _editingPeerIndex < 0 ? Iconsax.link : Iconsax.check,
                    size: 15,
                    color: jade400,
                  ),
                  label: Text(
                    _editingPeerIndex < 0 ? '添加设备' : '保存设备',
                    style: const TextStyle(color: jade400, fontSize: 12),
                  ),
                ),
              ),
              for (int index = 0; index < _manualPeers.length; index++)
                Container(
                  margin: const EdgeInsets.only(top: 4),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 6,
                  ),
                  decoration: BoxDecoration(
                    color: surfaceLowest,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: surfaceBorder),
                  ),
                  child: Row(
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
                                  color: textPrimary,
                                  fontSize: 12,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            Text(
                              formatManualEndpoint(_manualPeers[index]),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: textMuted,
                                fontSize: 11,
                                fontFamily: 'monospace',
                              ),
                            ),
                          ],
                        ),
                      ),
                      TextButton(
                        onPressed: () {
                          final ManualPeer peer = _manualPeers[index];
                          setState(() {
                            _manualNameController.text = peer.name;
                            _manualHostController.text = peer.host;
                            _manualPortController.text =
                                peer.port == 0 ? '' : peer.port.toString();
                            _editingPeerIndex = index;
                          });
                        },
                        child: const Text('编辑', style: TextStyle(fontSize: 11)),
                      ),
                      TextButton(
                        onPressed: () => setState(() {
                          _manualPeers.removeAt(index);
                          _editingPeerIndex = -1;
                        }),
                        child: const Text(
                          '删除',
                          style: TextStyle(color: dangerCoral, fontSize: 11),
                        ),
                      ),
                    ],
                  ),
                ),
              const SizedBox(height: 12),

              // 端到端加密开关
              _buildSwitchRow(
                title: '端到端加密',
                subtitle: '使用预共享密钥对传输内容加密；两端密钥需一致。',
                value: _encryptionEnabled,
                onChanged: (bool next) =>
                    setState(() => _encryptionEnabled = next),
              ),
              if (_encryptionEnabled) ...<Widget>[
                const SizedBox(height: 6),
                TextField(
                  controller: _secretController,
                  obscureText: true,
                  style: const TextStyle(
                    color: textPrimary,
                    fontSize: 12,
                    fontFamily: 'monospace',
                  ),
                  decoration: const InputDecoration(
                    hintText: '输入预共享加密口令（已保存则留空保持不变）',
                    isDense: true,
                    suffixIcon: Padding(
                      padding: EdgeInsets.all(12),
                      child: IconsaxIcon(
                        Iconsax.key,
                        size: 15,
                        color: badgeRoute,
                      ),
                    ),
                  ),
                ),
              ],
              const SizedBox(height: 4),

              // SSH 中继开关与配置
              _buildSwitchRow(
                title: 'SSH 反向隧道中继',
                subtitle: '经自有 SSH 服务器打通跨网连接(需在服务器放开端口)。',
                value: _sshEnabled,
                onChanged: (bool next) => setState(() => _sshEnabled = next),
              ),
              if (_sshEnabled) ...<Widget>[
                const SizedBox(height: 8),
                _buildSshSummaryCard(),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    key: const Key('open-ssh-relay'),
                    onPressed: widget.onOpenSshRelay,
                    icon: const IconsaxIcon(
                      Iconsax.terminal,
                      size: 17,
                      color: jade400,
                    ),
                    label: const Text('打开 SSH 中继控制台'),
                    style: FilledButton.styleFrom(
                      backgroundColor: surfaceActive,
                      foregroundColor: jade400,
                      minimumSize: const Size(0, 44),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ],
    );
  }

  /// SSH 配置摘要
  Widget _buildSshSummaryCard() {
    final InkSettings settings = widget.initialSettings;
    final bool hasFingerprint =
        freshHostFingerprint(settings.sshFingerprint) != null;
    final String host = settings.sshHost.isEmpty ? '未填写' : settings.sshHost;
    final String port = settings.sshPort > 0 ? '${settings.sshPort}' : '22';
    final String user = settings.sshUser.isEmpty ? '未填写' : settings.sshUser;
    final bool hasEndpoint =
        settings.sshHost.isNotEmpty && settings.sshUser.isNotEmpty;
    final String remote = settings.sshRemotePort == sshRemotePortAuto
        ? '自动分配'
        : '${settings.sshRemotePort}';

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: surfaceActive,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            '$host:$port · $user',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: textPrimary,
              fontSize: 12,
              fontWeight: FontWeight.w600,
              fontFamily: 'monospace',
            ),
          ),
          const SizedBox(height: 6),
          _buildSshCheckLine(
            '服务器与用户',
            hasEndpoint ? '已填写' : '未填齐',
            hasEndpoint,
          ),
          _buildSshCheckLine('远端监听端口', remote, hasEndpoint),
          _buildSshCheckLine(
            '主机指纹',
            hasFingerprint ? settings.sshFingerprint : '未固定',
            hasFingerprint,
          ),
        ],
      ),
    );
  }

  Widget _buildSshCheckLine(String label, String value, bool ready) {
    return Padding(
      padding: const EdgeInsets.only(top: 2),
      child: Row(
        children: <Widget>[
          IconsaxIcon(
            ready ? Iconsax.check : Iconsax.closeCircle,
            size: 13,
            color: ready ? jade400 : dangerCoral,
          ),
          const SizedBox(width: 6),
          Text(
            label,
            style: const TextStyle(color: textMuted, fontSize: 10.5),
          ),
          const Spacer(),
          Flexible(
            child: Text(
              value,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: ready ? textPrimary : dangerCoral,
                fontSize: 10.5,
                fontFamily: 'monospace',
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// 开关行：左侧标题 + 说明，右侧 Switch。
  Widget _buildSwitchRow({
    required String title,
    required String subtitle,
    required bool value,
    required ValueChanged<bool> onChanged,
  }) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                title,
                style: const TextStyle(color: textMuted, fontSize: 11),
              ),
              const SizedBox(height: 3),
              Text(
                subtitle,
                style: const TextStyle(color: textDim, fontSize: 10),
              ),
            ],
          ),
        ),
        const SizedBox(width: 10),
        Switch(
          value: value,
          thumbColor: WidgetStateProperty.resolveWith<Color?>(
            (Set<WidgetState> states) =>
                states.contains(WidgetState.selected) ? jade400 : null,
          ),
          trackColor: WidgetStateProperty.resolveWith<Color?>(
            (Set<WidgetState> states) => states.contains(WidgetState.selected)
                ? jade400.withValues(alpha: 0.35)
                : null,
          ),
          onChanged: onChanged,
        ),
      ],
    );
  }

  Future<void> _showUsageGuideDialog() async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (BuildContext dialogContext) {
        return AlertDialog(
          backgroundColor: surfaceContainer,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(18),
            side: const BorderSide(color: borderLuminescent),
          ),
          title: const Row(
            children: <Widget>[
              IconsaxIcon(Iconsax.layers, size: 18, color: jade400),
              SizedBox(width: 8),
              Text(
                '使用说明',
                style: TextStyle(
                  color: textPrimary,
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
          content: ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 360),
            child: SingleChildScrollView(
              child: SelectableText(
                '1. 局域网传输\n'
                '手机与电脑或另一台手机连接同一 Wi-Fi（或手机开启热点），设备会自动出现在「探索」雷达页，点按设备即可选文件或文件夹发送。\n\n'
                '2. 跨网络传输（三种方式）\n'
                '• Tailscale 固定节点：两端登录同一 Tailscale 账号后，在本页「Tailscale / 固定探测节点」填入对方 IP 或 MagicDNS 名称，保存后像局域网一样直接点设备发送。\n'
                '• 一次性短码：不在同一网络时，切换到「配对」页选择文件生成短码或二维码，对方输入短码或扫码即可点对点接收。\n'
                '• SSH 反向隧道中继：开启「SSH 反向隧道中继」并进入中继控制台填写服务器与配对码，两端通过自有服务器自动发现。\n\n'
                '3. 代理与 VPN 分流提示\n'
                '手机开启 Clash / Surge / Shadowrocket 等 TUN 模式代理时，UDP 广播端口可能被改写导致局域网互不相见。请将「墨洞 (InkHole)」加入代理软件的分流白名单（绕过代理 / 直连）。\n\n'
                '4. 文件保存在哪\n'
                '接收的文件先保存在应用内收件箱；在「收件箱」页点按导出或分享即可存入系统「下载 (Download/InkHole)」或任意指定目录。',
                style: const TextStyle(
                  color: textMuted,
                  fontSize: 12.5,
                  height: 1.5,
                ),
              ),
            ),
          ),
          actions: <Widget>[
            ElevatedButton(
              onPressed: () => Navigator.of(dialogContext).pop(),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                foregroundColor: bgAbyss,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text(
                '我知道了',
                style: TextStyle(fontWeight: FontWeight.w700),
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _buildUpdateSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const Row(
          children: <Widget>[
            IconsaxIcon(Iconsax.update, color: jade400, size: 15),
            SizedBox(width: 6),
            Text(
              '帮助与版本',
              style: TextStyle(
                color: textPrimary,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: surfaceContainer,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: surfaceBorder),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Row(
                children: <Widget>[
                  const Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Text(
                          '当前版本',
                          style: TextStyle(color: textMuted, fontSize: 11),
                        ),
                        SizedBox(height: 2),
                        Text(
                          'v$appVersion',
                          style: TextStyle(
                            color: badgeRoute,
                            fontSize: 13,
                            fontFamily: 'monospace',
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                  ),
                  SizedBox(
                    height: 36,
                    child: OutlinedButton.icon(
                      onPressed: _showUsageGuideDialog,
                      icon: const IconsaxIcon(
                        Iconsax.layers,
                        size: 14,
                        color: textPrimary,
                      ),
                      label: const Text(
                        '使用说明',
                        style: TextStyle(
                          color: textPrimary,
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      style: OutlinedButton.styleFrom(
                        backgroundColor: surfaceHigh,
                        side: const BorderSide(color: surfaceBorder),
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(18),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  SizedBox(
                    height: 36,
                    child: ElevatedButton(
                      onPressed: _checkingUpdate ? null : _checkUpdate,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: surfaceActive,
                        elevation: 0,
                        padding: const EdgeInsets.symmetric(horizontal: 14),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(18),
                          side: const BorderSide(color: borderLuminescent),
                        ),
                      ),
                      child: Text(
                        _checkingUpdate ? '正在检查…' : '检查更新',
                        style: const TextStyle(
                          color: jade400,
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              const Text(
                '发现新版本后直接在应用内下载。Android 下载完成后拉起安装。',
                style: TextStyle(color: textDim, fontSize: 10),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildBottomActionBar() {
    return Row(
      children: <Widget>[
        Expanded(
          flex: 1,
          child: SizedBox(
            height: 44,
            child: ElevatedButton(
              onPressed: widget.onBack,
              style: ElevatedButton.styleFrom(
                backgroundColor: surfaceHigh,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(22),
                  side: const BorderSide(color: surfaceBorder),
                ),
              ),
              child: const Text(
                '返回',
                style: TextStyle(color: textMuted, fontSize: 13),
              ),
            ),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          flex: 2,
          child: SizedBox(
            height: 44,
            child: FilledButton.icon(
              key: const Key('save-settings-btn'),
              onPressed: _isSaving ? null : _handleSave,
              icon: _isSaving
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        color: bgAbyss,
                      ),
                    )
                  : const IconsaxIcon(
                      Iconsax.check,
                      size: 18,
                      color: bgAbyss,
                      secondaryOpacity: 0.25,
                    ),
              label: Text(
                _isSaving ? '应用中...' : '保存配置',
                style: const TextStyle(
                  color: bgAbyss,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
              style: FilledButton.styleFrom(
                backgroundColor: jade400,
                foregroundColor: bgAbyss,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(22),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}
