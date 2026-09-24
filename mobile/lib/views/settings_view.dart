import 'package:flutter/material.dart';
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
  });

  final InkSettings initialSettings;
  final String instanceId;
  final int actualPort;
  final String inboxPath;
  final VoidCallback onBack;
  final ValueChanged<InkSettings> onSave;
  final VoidCallback onChooseDirectory;
  final VoidCallback onResetDirectory;

  @override
  State<SettingsView> createState() => _SettingsViewState();
}

class _SettingsViewState extends State<SettingsView> {
  late final TextEditingController _nameController;
  late final TextEditingController _portController;
  late final TextEditingController _relayController;
  late final TextEditingController _tailscaleController;

  bool _isSaving = false;

  @override
  void initState() {
    super.initState();
    _nameController = TextEditingController(text: widget.initialSettings.peerName);
    _portController = TextEditingController(
      text: widget.initialSettings.listenPort > 0
          ? widget.initialSettings.listenPort.toString()
          : '',
    );
    _relayController = TextEditingController(text: widget.initialSettings.transitRelay);
    _tailscaleController = TextEditingController(
      text: widget.initialSettings.manualPeers.isNotEmpty
          ? widget.initialSettings.manualPeers.first.host
          : '',
    );
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
    setState(() => _isSaving = true);

    final int port = int.tryParse(_portController.text.trim()) ?? 0;
    final String tailHost = _tailscaleController.text.trim();
    final List<ManualPeer> manualPeers = tailHost.isNotEmpty
        ? <ManualPeer>[ManualPeer(name: 'Tailscale', host: tailHost)]
        : <ManualPeer>[];

    final InkSettings updated = InkSettings(
      peerName: _nameController.text.trim().isNotEmpty
          ? _nameController.text.trim()
          : 'InkHole',
      listenPort: port,
      encryptionEnabled: widget.initialSettings.encryptionEnabled,
      secret: widget.initialSettings.secret,
      manualPeers: manualPeers,
      rendezvousUrl: widget.initialSettings.rendezvousUrl,
      transitRelay: _relayController.text.trim(),
      sshEnabled: widget.initialSettings.sshEnabled,
      sshHost: widget.initialSettings.sshHost,
      sshPort: widget.initialSettings.sshPort,
      sshUser: widget.initialSettings.sshUser,
      sshFingerprint: widget.initialSettings.sshFingerprint,
      sshPrivateKey: widget.initialSettings.sshPrivateKey,
      sshPassphrase: widget.initialSettings.sshPassphrase,
    );

    widget.onSave(updated);

    await Future<void>.delayed(const Duration(milliseconds: 600));
    if (mounted) {
      setState(() => _isSaving = false);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('配置已保存并生效'),
          backgroundColor: surfaceActive,
          duration: Duration(seconds: 2),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final String shortId = widget.instanceId.length > 8
        ? widget.instanceId.substring(0, 8)
        : (widget.instanceId.isEmpty ? 'node-4f89' : widget.instanceId);

    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: textMuted),
          onPressed: widget.onBack,
        ),
        title: const Text(
          'Settings',
          style: TextStyle(
            color: textPrimary,
            fontSize: 16,
            fontWeight: FontWeight.w700,
          ),
        ),
        actions: <Widget>[
          IconButton(
            onPressed: () {},
            icon: const Icon(Icons.language, size: 20),
            color: textMuted,
            splashRadius: 18,
          ),
          IconButton(
            onPressed: () {},
            icon: const Icon(Icons.settings, size: 20),
            color: textMuted,
            splashRadius: 18,
          ),
        ],
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
            _buildTopologyCard(),
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
                    '节点参数与量子加密传输网络配置',
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
                      'localhost-$shortId',
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
                          '系统内核',
                          style: TextStyle(color: textDim, fontSize: 11),
                        ),
                      ],
                    ),
                    const Text(
                      'v2.0.14',
                      style: TextStyle(
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
                      '${widget.actualPort} (固定信道)',
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
                  Text(
                    'READ / WRITE',
                    style: TextStyle(
                      color: textDim,
                      fontSize: 9,
                      fontFamily: 'monospace',
                    ),
                  ),
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
                        widget.inboxPath.isNotEmpty
                            ? widget.inboxPath
                            : '/storage/emulated/0/Download/InkHole',
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
                '所有接收文件和文件夹统一保存在这里；未自定义时保存到系统下载目录的 InkHole 文件夹。',
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
                  Text(
                    'FALLBACK',
                    style: TextStyle(
                      color: badgeRoute,
                      fontSize: 9,
                      fontFamily: 'monospace',
                    ),
                  ),
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
                  Text(
                    '100.x.x.x',
                    style: TextStyle(
                      color: badgeRoute,
                      fontSize: 9,
                      fontFamily: 'monospace',
                    ),
                  ),
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
                  hintText: '例如 100.86.14.21:41300',
                  suffixIcon: Icon(Icons.vpn_key, size: 16, color: textDim),
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                '配置虚拟局域网后可绕过 NAT 探测，直连打洞秒级就绪。',
                style: TextStyle(color: textDim, fontSize: 10),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildTopologyCard() {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: borderLuminescent),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          const Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Text(
                '墨洞原语拓扑校验',
                style: TextStyle(
                  color: textPrimary,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              Text(
                'SHA256 SYNC',
                style: TextStyle(
                  color: textDim,
                  fontSize: 9,
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(10),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: <Widget>[
                const Text(
                  'Ed25519-P2P::INK-NODE-READY',
                  style: TextStyle(
                    color: textMuted,
                    fontSize: 11,
                    fontFamily: 'monospace',
                  ),
                ),
                Container(
                  width: 7,
                  height: 7,
                  decoration: const BoxDecoration(
                    color: jade400,
                    shape: BoxShape.circle,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
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
                '取消',
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
