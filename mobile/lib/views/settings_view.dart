import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/updater.dart';
import '../models.dart';
import '../theme.dart';

/// Tab 4: 设置视图 (Settings View)
class SettingsView extends StatefulWidget {
  const SettingsView({
    super.key,
    required this.initialSettings,
    required this.instanceId,
    required this.actualPort,
    required this.inboxPath,
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
  late final TextEditingController _relayController;
  late final TextEditingController _tailscaleController;

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
    _relayController = TextEditingController(text: widget.initialSettings.transitRelay);
    _tailscaleController = TextEditingController(text: _tailscaleText(widget.initialSettings));
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
      _relayController,
      oldWidget.initialSettings.transitRelay,
      widget.initialSettings.transitRelay,
    );
    _syncIfUntouched(
      _tailscaleController,
      _tailscaleText(oldWidget.initialSettings),
      _tailscaleText(widget.initialSettings),
    );
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

  String _tailscaleText(InkSettings settings) {
    for (final ManualPeer peer in settings.manualPeers) {
      if (peer.name == 'Tailscale') {
        return formatManualEndpoint(peer);
      }
    }
    return '';
  }

  @override
  void dispose() {
    _nameController.dispose();
    _portController.dispose();
    _relayController.dispose();
    _tailscaleController.dispose();
    super.dispose();
  }

  Future<void> _handleSave() async {
    if (_isSaving) return;

    final int port = int.tryParse(_portController.text.trim()) ?? 0;
    if (port != 0 && (port < 1 || port > 65535)) {
      await _showNotice('端口无效', '监听端口需在 1-65535 之间，留空表示自动选择。');
      return;
    }
    final String relay = _relayController.text.trim();
    if (relay.isNotEmpty && !_isValidHostPort(relay)) {
      await _showNotice('中继地址无效', '格式应为 host:port，例如 transit.magic-wormhole.io:4001。');
      return;
    }
    final ManualPeer tailscale = parseManualEndpoint(
      _tailscaleController.text.trim(),
      name: 'Tailscale',
    );
    if (_tailscaleController.text.trim().isNotEmpty && tailscale.host.isEmpty) {
      await _showNotice('Tailscale 地址无效', '格式应为主机名或 IP（IPv6 可用 [::1] 形式），可带端口。');
      return;
    }

    setState(() => _isSaving = true);
    final List<ManualPeer> manualPeers = <ManualPeer>[
      ...widget.initialSettings.manualPeers.where(
        (ManualPeer peer) => peer.name != 'Tailscale',
      ),
      if (tailscale.host.isNotEmpty) tailscale,
    ];

    final InkSettings updated = InkSettings(
      peerName: _nameController.text.trim().isNotEmpty
          ? _nameController.text.trim()
          : 'InkHole',
      listenPort: port,
      encryptionEnabled: _encryptionEnabled,
      secret: widget.initialSettings.secret,
      manualPeers: manualPeers,
      rendezvousUrl: widget.initialSettings.rendezvousUrl,
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
    final String shortId = widget.instanceId.length > 8
        ? widget.instanceId.substring(0, 8)
        : (widget.instanceId.isEmpty ? '生成中' : widget.instanceId);

    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: textMuted),
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
            _buildTelemetryHeroCard(shortId),
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

            // 分区 4：技术身份拓扑参考卡片
            _buildUpdateSection(),
            const SizedBox(height: 18),

            // 底部操作胶囊栏 (取消 / 保存)
            _buildBottomActionBar(),
            const SizedBox(height: 28),
          ],
        ),
      ),
    );
  }

  Widget _buildTelemetryHeroCard(String shortId) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(16),
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
                        '设置',
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
                    '本机名称、收件目录和跨网中继',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                ],
              ),
              Container(
                width: 36,
                height: 36,
                decoration: BoxDecoration(
                  color: surfaceHigh,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: const Icon(Icons.tune, color: jade400, size: 20),
              ),
            ],
          ),
          const SizedBox(height: 12),

          // 元数据三行条目
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(10),
            ),
            child: Column(
              children: <Widget>[
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    const Row(
                      children: <Widget>[
                        Icon(Icons.fingerprint, color: textMuted, size: 14),
                        SizedBox(width: 6),
                        Text(
                          '本机标识',
                          style: TextStyle(color: textDim, fontSize: 11),
                        ),
                      ],
                    ),
                    Text(
                      shortId,
                      style: const TextStyle(
                        color: textPrimary,
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
                        Icon(Icons.layers_outlined, color: textMuted, size: 14),
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
                        Icon(Icons.hub_outlined, color: textMuted, size: 14),
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
              '建议自定义 1024–49151 固定端口',
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
            Icon(Icons.dns_outlined, color: jade400, size: 15),
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
            borderRadius: BorderRadius.circular(14),
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
                  suffixIcon: Icon(Icons.edit, size: 16, color: jade400),
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
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
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
                  suffixIcon: Icon(Icons.lock_open, size: 16, color: textDim),
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
            Icon(Icons.folder_open, color: jade400, size: 15),
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
            borderRadius: BorderRadius.circular(14),
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
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Row(
                  children: <Widget>[
                    const Icon(Icons.snippet_folder_outlined, color: jade400, size: 16),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        widget.inboxPath.isNotEmpty ? widget.inboxPath : '应用内收件箱',
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
                    icon: const Icon(Icons.restart_alt, size: 14, color: textMuted),
                    label: const Text(
                      '恢复默认',
                      style: TextStyle(color: textMuted, fontSize: 11),
                    ),
                  ),
                  const SizedBox(width: 8),
                  ElevatedButton.icon(
                    onPressed: widget.onChooseDirectory,
                    icon: const Icon(Icons.drive_file_move, size: 14, color: jade400),
                    label: const Text(
                      '选择目录',
                      style: TextStyle(color: jade400, fontSize: 11),
                    ),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: surfaceHigh,
                      elevation: 0,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(8),
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
            Icon(Icons.alt_route, color: jade400, size: 15),
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
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: surfaceBorder),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              // Wormhole 中继
              const Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: <Widget>[
                  Text(
                    'Wormhole 中继穿透服务器',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                  SizedBox.shrink(),
                ],
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
                  suffixIcon: Icon(Icons.public, size: 16, color: textDim),
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                '支持自建 VPS 穿透节点以实现高带宽端到端隧道中继。',
                style: TextStyle(color: textDim, fontSize: 10),
              ),
              const SizedBox(height: 12),

              // Tailscale 固定 IP
              const Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: <Widget>[
                  Text(
                    'Tailscale 固定对端 IP',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                  SizedBox.shrink(),
                ],
              ),
              const SizedBox(height: 6),
              TextField(
                controller: _tailscaleController,
                style: const TextStyle(
                  color: textPrimary,
                  fontSize: 12,
                  fontFamily: 'monospace',
                ),
                decoration: const InputDecoration(
                  hintText: '例如 100.86.14.21',
                  suffixIcon: Icon(Icons.vpn_key, size: 16, color: textDim),
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                '配置虚拟局域网后可绕过 NAT 探测，直连打洞秒级就绪。',
                style: TextStyle(color: textDim, fontSize: 10),
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
                    icon: const Icon(Icons.terminal, size: 17),
                    label: const Text('打开 SSH 中继控制台'),
                    style: FilledButton.styleFrom(
                      backgroundColor: surfaceActive,
                      foregroundColor: jade400,
                      minimumSize: const Size(0, 44),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(10),
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

  /// SSH 配置摘要：连接参数与私钥都在控制台里编辑，这里只显示当前值是否齐备。
  /// 私钥不在这里显示——本页拿到的 `sshPrivateKey` 恒为空串（home_page 不把
  /// 密钥下发给设置页），据此判断"是否已导入"只会得出永远错误的结论。
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
        borderRadius: BorderRadius.circular(10),
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
          _buildSshCheckLine('服务器与用户', hasEndpoint ? '已填写' : '未填齐', hasEndpoint),
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
          Icon(
            ready ? Icons.check_circle_outline : Icons.error_outline,
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
          // 用 WidgetStateProperty 指定选中色:activeColor 在 3.29 已弃用，
          // activeThumbColor 又要 3.27+，这个写法两个版本都无告警。
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

  /// 带标题的输入框。
  Widget _buildUpdateSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        const Row(
          children: <Widget>[
            Icon(Icons.system_update_alt, color: jade400, size: 15),
            SizedBox(width: 6),
            Text(
              '版本',
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
            borderRadius: BorderRadius.circular(14),
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
            child: ElevatedButton.icon(
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
                  : const Icon(Icons.check_circle, size: 18, color: bgAbyss),
              label: Text(
                _isSaving ? '应用中...' : '保存配置',
                style: const TextStyle(
                  color: bgAbyss,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                elevation: 4,
                shadowColor: jade400.withValues(alpha: 0.35),
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
