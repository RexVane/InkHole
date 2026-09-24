import 'package:flutter/material.dart';
import '../models.dart';
import '../theme.dart';
import '../widgets/cyber_radar.dart';
import '../widgets/device_orbit_node.dart';

/// Tab 0: 量子雷达主控视图 (Transfer Radar)
class RadarView extends StatelessWidget {
  const RadarView({
    super.key,
    required this.listenPort,
    required this.identityFingerprint,
    required this.peers,
    required this.selectedPeerInstance,
    required this.searching,
    required this.transferPercent,
    required this.recentReceived,
    required this.onSelectPeer,
    required this.onTapCore,
    required this.onSendFile,
    required this.onOpenWormhole,
    required this.onOpenPairQr,
    required this.onOpenSettings,
    required this.onRefreshDiscovery,
    required this.onOpenInbox,
    required this.onOpenFile,
  });

  final int listenPort;
  final String identityFingerprint;
  final List<PeerView> peers;
  final String? selectedPeerInstance;
  final bool searching;
  final double transferPercent;
  final List<ReceivedFile> recentReceived;

  final ValueChanged<PeerView> onSelectPeer;
  final VoidCallback onTapCore;
  final VoidCallback onSendFile;
  final VoidCallback onOpenWormhole;
  final VoidCallback onOpenPairQr;
  final VoidCallback onOpenSettings;
  final VoidCallback onRefreshDiscovery;
  final VoidCallback onOpenInbox;
  final ValueChanged<ReceivedFile> onOpenFile;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: <Widget>[
        // 1. 顶部品牌与操作栏
        _buildTopHeader(context),

        // 2. 中间雷达探测核心区域
        Expanded(
          child: SingleChildScrollView(
            physics: const BouncingScrollPhysics(),
            child: Column(
              children: <Widget>[
                const SizedBox(height: 10),
                _buildRadarSection(),
                const SizedBox(height: 16),
                _buildStatusFooter(),
                const SizedBox(height: 16),
                _buildQuickActions(),
                const SizedBox(height: 20),
              ],
            ),
          ),
        ),

        // 3. 底部近期收发任务抽屉
        _buildTransfersDrawer(context),
      ],
    );
  }

  Widget _buildTopHeader(BuildContext context) {
    final String shortFp = identityFingerprint.length > 8
        ? identityFingerprint.substring(0, 8)
        : (identityFingerprint.isEmpty ? 'ed25519' : identityFingerprint);

    return Container(
      padding: const EdgeInsets.fromLTRB(18, 6, 12, 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              // 品牌标题
              Row(
                crossAxisAlignment: CrossAxisAlignment.baseline,
                textBaseline: TextBaseline.alphabetic,
                children: <Widget>[
                  const Text(
                    '墨洞',
                    style: TextStyle(
                      color: textPrimary,
                      fontSize: 24,
                      fontWeight: FontWeight.w900,
                      letterSpacing: 1.5,
                      shadows: <Shadow>[
                        Shadow(color: jade400, blurRadius: 16),
                      ],
                    ),
                  ),
                  const SizedBox(width: 6),
                  Text(
                    'InkHole',
                    style: TextStyle(
                      color: jade400.withValues(alpha: 0.9),
                      fontSize: 12,
                      fontFamily: 'monospace',
                      fontWeight: FontWeight.w600,
                      letterSpacing: 1.2,
                    ),
                  ),
                ],
              ),

              // 右侧操作按钮
              Row(
                children: <Widget>[
                  IconButton(
                    onPressed: onRefreshDiscovery,
                    icon: const Icon(Icons.refresh, size: 21),
                    color: textMuted,
                    splashRadius: 18,
                    tooltip: '刷新雷达扫描',
                  ),
                  IconButton(
                    onPressed: onOpenWormhole,
                    icon: const Icon(Icons.language, size: 21),
                    color: textMuted,
                    splashRadius: 18,
                    tooltip: '跨网虫洞直连',
                  ),
                  IconButton(
                    onPressed: onOpenSettings,
                    icon: const Icon(Icons.settings_outlined, size: 21),
                    color: textMuted,
                    splashRadius: 18,
                    tooltip: '墨洞配置',
                  ),
                ],
              ),
            ],
          ),
          const SizedBox(height: 6),

          // 局域网广播状态条
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            decoration: BoxDecoration(
              color: surfaceLowest.withValues(alpha: 0.9),
              borderRadius: BorderRadius.circular(20),
              border: Border.all(color: borderLuminescent),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Container(
                  width: 6,
                  height: 6,
                  decoration: const BoxDecoration(
                    color: jade400,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 6),
                const Text(
                  '局域网广播: ',
                  style: TextStyle(color: textMuted, fontSize: 10),
                ),
                Text(
                  ':$listenPort',
                  style: const TextStyle(
                    color: jade400,
                    fontSize: 10,
                    fontFamily: 'monospace',
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const Text(
                  '  |  ',
                  style: TextStyle(color: textDim, fontSize: 10),
                ),
                Text(
                  'QUIC · $shortFp',
                  style: const TextStyle(
                    color: textMuted,
                    fontSize: 10,
                    fontFamily: 'monospace',
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildRadarSection() {
    // 根据当前发现的设备计算环绕轨道上的位置
    final List<Widget> orbitNodes = <Widget>[];

    if (peers.isNotEmpty) {
      // 节点 1：右上
      orbitNodes.add(
        DeviceOrbitNode(
          peer: peers[0],
          isSelected: peers[0].instanceId == selectedPeerInstance,
          top: -2,
          right: -8,
          onTap: () => onSelectPeer(peers[0]),
        ),
      );
    }
    if (peers.length > 1) {
      // 节点 2：左下
      orbitNodes.add(
        DeviceOrbitNode(
          peer: peers[1],
          isSelected: peers[1].instanceId == selectedPeerInstance,
          bottom: -4,
          left: -6,
          onTap: () => onSelectPeer(peers[1]),
        ),
      );
    }
    if (peers.length > 2) {
      // 节点 3：左上
      orbitNodes.add(
        DeviceOrbitNode(
          peer: peers[2],
          isSelected: peers[2].instanceId == selectedPeerInstance,
          top: 30,
          left: -14,
          onTap: () => onSelectPeer(peers[2]),
        ),
      );
    }

    return CyberRadar(
      transferPercent: transferPercent,
      searching: searching,
      orbitChildren: orbitNodes,
      onTapCore: onTapCore,
    );
  }

  Widget _buildStatusFooter() {
    final String statusText = peers.isEmpty
        ? '等待附近的墨洞上线···'
        : '已锁定 ${peers.length} 台墨洞终端 · 轻点发送';

    final String shortKey = identityFingerprint.length > 12
        ? '${identityFingerprint.substring(0, 4)}..${identityFingerprint.substring(identityFingerprint.length - 4)}'
        : identityFingerprint;

    return Column(
      children: <Widget>[
        Text(
          statusText,
          style: const TextStyle(
            color: textPrimary,
            fontSize: 13,
            fontWeight: FontWeight.w600,
            letterSpacing: 1.2,
            shadows: <Shadow>[
              Shadow(color: jade400, blurRadius: 10),
            ],
          ),
        ),
        const SizedBox(height: 4),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            const Text(
              '广播密钥: ',
              style: TextStyle(color: textDim, fontSize: 10),
            ),
            Text(
              'ed25519:$shortKey',
              style: const TextStyle(
                color: textMuted,
                fontSize: 10,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildQuickActions() {
    return Container(
      constraints: const BoxConstraints(maxWidth: 340),
      padding: const EdgeInsets.symmetric(horizontal: 14),
      child: Row(
        children: <Widget>[
          // 发文件按钮 (高亮翡翠绿)
          Expanded(
            child: SizedBox(
              height: 40,
              child: ElevatedButton.icon(
                onPressed: onSendFile,
                icon: const Icon(Icons.arrow_upward, size: 16, color: bgAbyss),
                label: const Text(
                  '发文件',
                  style: TextStyle(
                    color: bgAbyss,
                    fontSize: 12,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                style: ElevatedButton.styleFrom(
                  backgroundColor: jade400,
                  elevation: 6,
                  shadowColor: jade400.withValues(alpha: 0.35),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),

          // 暗号直连
          Expanded(
            child: SizedBox(
              height: 40,
              child: ElevatedButton.icon(
                onPressed: onOpenWormhole,
                icon: const Icon(Icons.key, size: 15, color: jade400),
                label: const Text(
                  '暗号直连',
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                style: ElevatedButton.styleFrom(
                  backgroundColor: surfaceContainer.withValues(alpha: 0.9),
                  elevation: 0,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                    side: const BorderSide(color: surfaceBorder),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),

          // 扫码配对
          Expanded(
            child: SizedBox(
              height: 40,
              child: ElevatedButton.icon(
                onPressed: onOpenPairQr,
                icon: const Icon(Icons.qr_code_scanner, size: 15, color: jade400),
                label: const Text(
                  '扫码配对',
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                style: ElevatedButton.styleFrom(
                  backgroundColor: surfaceContainer.withValues(alpha: 0.9),
                  elevation: 0,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                    side: const BorderSide(color: surfaceBorder),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildTransfersDrawer(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
      decoration: BoxDecoration(
        color: surfaceContainer.withValues(alpha: 0.95),
        borderRadius: const BorderRadius.vertical(top: Radius.circular(22)),
        border: const Border(
          top: BorderSide(color: borderLuminescent, width: 1.0),
        ),
        boxShadow: const <BoxShadow>[
          BoxShadow(
            color: Color(0x66000000),
            blurRadius: 20,
            offset: Offset(0, -4),
          ),
        ],
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          // 顶部小把手
          Container(
            width: 36,
            height: 3,
            margin: const EdgeInsets.only(bottom: 10),
            decoration: BoxDecoration(
              color: textDim,
              borderRadius: BorderRadius.circular(2),
            ),
          ),

          // 标题行与收件箱链接
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Row(
                children: <Widget>[
                  const Text(
                    '近期收发',
                    style: TextStyle(
                      color: textPrimary,
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(width: 6),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: surfaceHigh,
                      borderRadius: BorderRadius.circular(6),
                      border: Border.all(color: borderLuminescent),
                    ),
                    child: const Text(
                      '已接收',
                      style: TextStyle(
                        color: jade400,
                        fontSize: 9,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                  const SizedBox(width: 6),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: surfaceHigh,
                      borderRadius: BorderRadius.circular(6),
                      border: Border.all(color: surfaceBorder),
                    ),
                    child: Text(
                      '${recentReceived.length} 个任务',
                      style: const TextStyle(
                        color: textMuted,
                        fontSize: 9,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ),
                ],
              ),
              GestureDetector(
                onTap: onOpenInbox,
                child: const Row(
                  children: <Widget>[
                    Icon(Icons.folder_open, size: 14, color: jade400),
                    SizedBox(width: 4),
                    Text(
                      '收件箱',
                      style: TextStyle(
                        color: jade400,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),

          // 最近任务列表 (展示最多 2 条)
          if (recentReceived.isEmpty)
            Container(
              padding: const EdgeInsets.symmetric(vertical: 14),
              alignment: Alignment.center,
              child: const Text(
                '暂无近期传输任务',
                style: TextStyle(color: textDim, fontSize: 11),
              ),
            )
          else
            ...recentReceived.take(2).map((ReceivedFile file) {
              return Container(
                margin: const EdgeInsets.only(bottom: 8),
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                decoration: BoxDecoration(
                  color: surfaceLowest.withValues(alpha: 0.8),
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: surfaceBorder),
                ),
                child: Row(
                  children: <Widget>[
                    Container(
                      width: 34,
                      height: 34,
                      decoration: BoxDecoration(
                        color: surfaceHigh,
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(color: borderLuminescent),
                      ),
                      child: const Icon(
                        Icons.insert_drive_file_outlined,
                        color: jade400,
                        size: 18,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: <Widget>[
                          Text(
                            file.name,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: textPrimary,
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Row(
                            children: <Widget>[
                              Text(
                                formatBytes(file.size),
                                style: const TextStyle(
                                  color: textMuted,
                                  fontSize: 10,
                                  fontFamily: 'monospace',
                                ),
                              ),
                              const Text(' · ', style: TextStyle(color: textDim, fontSize: 10)),
                              const Text(
                                'BLAKE3 校验完成',
                                style: TextStyle(color: textDim, fontSize: 10),
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                    InkWell(
                      onTap: () => onOpenFile(file),
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(
                          color: jade400.withValues(alpha: 0.1),
                          borderRadius: BorderRadius.circular(6),
                          border: Border.all(color: jade400.withValues(alpha: 0.25)),
                        ),
                        child: const Text(
                          '已接收',
                          style: TextStyle(color: jade300, fontSize: 10),
                        ),
                      ),
                    ),
                  ],
                ),
              );
            }),
        ],
      ),
    );
  }
}
