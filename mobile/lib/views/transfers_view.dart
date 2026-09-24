import 'package:flutter/material.dart';
import '../models.dart';
import '../theme.dart';
import '../widgets/throughput_chart.dart';

/// Tab 1: 收发详情与传输队列视图 (Transfers Detail & Active Stream)
class TransfersView extends StatelessWidget {
  const TransfersView({
    super.key,
    required this.activeProgress,
    required this.speedBytesPerSec,
    required this.speedHistory,
    required this.listenPort,
    required this.receivedFiles,
    required this.isTransferPaused,
    required this.onTogglePause,
    required this.onCancelTransfer,
    required this.onClearHistory,
    required this.onOpenFile,
    required this.onRefresh,
    required this.onOpenPairQr,
    required this.onOpenSettings,
  });

  final TransferProgress? activeProgress;
  final double speedBytesPerSec;
  final List<double> speedHistory;
  final int listenPort;
  final List<ReceivedFile> receivedFiles;
  final bool isTransferPaused;

  final VoidCallback onTogglePause;
  final VoidCallback onCancelTransfer;
  final VoidCallback onClearHistory;
  final ValueChanged<ReceivedFile> onOpenFile;
  final VoidCallback onRefresh;
  final VoidCallback onOpenPairQr;
  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context) {
    final double speedMb = speedBytesPerSec / (1024 * 1024);
    final double peakSpeed = speedHistory.isEmpty
        ? speedMb
        : speedHistory.reduce((double a, double b) => a > b ? a : b);

    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        title: Row(
          children: <Widget>[
            const Text(
              '墨洞',
              style: TextStyle(
                color: textPrimary,
                fontSize: 20,
                fontWeight: FontWeight.w900,
                letterSpacing: 1.2,
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
              ),
            ),
          ],
        ),
        actions: <Widget>[
          IconButton(
            onPressed: onRefresh,
            icon: const Icon(Icons.refresh, size: 21),
            color: textMuted,
            splashRadius: 18,
          ),
          IconButton(
            onPressed: onOpenPairQr,
            icon: const Icon(Icons.language, size: 21),
            color: textMuted,
            splashRadius: 18,
          ),
          IconButton(
            onPressed: onOpenSettings,
            icon: const Icon(Icons.settings_outlined, size: 21),
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
            // 1. 活跃传输聚光灯 Hero 卡片
            _buildActiveTransferSpotlight(context, speedMb, peakSpeed),
            const SizedBox(height: 18),

            // 2. 传输队列与历史列表
            _buildQueueAndHistorySection(context),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }

  Widget _buildActiveTransferSpotlight(
    BuildContext context,
    double speedMb,
    double peakSpeed,
  ) {
    final bool hasActive = activeProgress != null && activeProgress!.total > 0;
    final double fraction = hasActive ? activeProgress!.fraction : 0.0;
    final int percent = (fraction * 100).toInt();

    final String filename = hasActive ? activeProgress!.filename : '待机空闲中';
    final String sizeInfo = hasActive
        ? '${formatBytes(activeProgress!.done)} / ${formatBytes(activeProgress!.total)}'
        : '0 B / 0 B';

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: borderLuminescent),
        boxShadow: const <BoxShadow>[
          BoxShadow(
            color: Color(0x66000000),
            blurRadius: 20,
            offset: Offset(0, 4),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          // 状态与加密指示
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Container(
                    width: 22,
                    height: 22,
                    decoration: BoxDecoration(
                      color: surfaceActive,
                      shape: BoxShape.circle,
                    ),
                    child: const Icon(Icons.cloud_sync, color: jade400, size: 14),
                  ),
                  const SizedBox(width: 6),
                  Text(
                    hasActive ? 'TRANSFERRING · QUIC_STREAM' : 'STANDBY · READY',
                    style: const TextStyle(
                      color: jade400,
                      fontSize: 10,
                      fontFamily: 'monospace',
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.6,
                    ),
                  ),
                ],
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: surfaceActive,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: borderLuminescent),
                ),
                child: Row(
                  children: <Widget>[
                    Container(
                      width: 5,
                      height: 5,
                      decoration: const BoxDecoration(
                        color: jade400,
                        shape: BoxShape.circle,
                      ),
                    ),
                    const SizedBox(width: 5),
                    const Text(
                      'TLS 1.3 ENCRYPTED',
                      style: TextStyle(
                        color: badgeRoute,
                        fontSize: 9,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),

          // 主文件名与百分比
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: surfaceActive,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: borderLuminescent),
                ),
                child: const Icon(
                  Icons.folder_zip_outlined,
                  color: jade400,
                  size: 22,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(
                      filename,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: textPrimary,
                        fontSize: 14,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Row(
                      children: <Widget>[
                        Text(
                          sizeInfo,
                          style: const TextStyle(
                            color: textMuted,
                            fontSize: 10,
                            fontFamily: 'monospace',
                          ),
                        ),
                        if (hasActive) ...<Widget>[
                          const Text(' · ', style: TextStyle(color: textDim, fontSize: 10)),
                          const Text(
                            '传输中',
                            style: TextStyle(
                              color: badgeRoute,
                              fontSize: 10,
                            ),
                          ),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
              Text(
                '$percent%',
                style: const TextStyle(
                  color: jade400,
                  fontSize: 22,
                  fontFamily: 'monospace',
                  fontWeight: FontWeight.w800,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),

          // 渐变进度条
          Stack(
            children: <Widget>[
              Container(
                height: 6,
                width: double.infinity,
                decoration: BoxDecoration(
                  color: surfaceLowest,
                  borderRadius: BorderRadius.circular(3),
                ),
              ),
              FractionallySizedBox(
                widthFactor: fraction.clamp(0.01, 1.0),
                child: Container(
                  height: 6,
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(3),
                    gradient: const LinearGradient(
                      colors: <Color>[secondaryTeal, jade400],
                    ),
                    boxShadow: <BoxShadow>[
                      BoxShadow(
                        color: jade400.withValues(alpha: 0.6),
                        blurRadius: 8,
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),

          // 校验与断点提示
          const Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Text(
                'BLAKE3 树状并行块流水线',
                style: TextStyle(color: textDim, fontSize: 10),
              ),
              Text(
                '断点续传就绪',
                style: TextStyle(
                  color: badgeRoute,
                  fontSize: 10,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),

          // 实时吞吐量波形图 (Sparkline)
          ThroughputChart(
            speeds: speedHistory,
            currentSpeedMb: speedMb,
            peakSpeedMb: peakSpeed,
          ),
          const SizedBox(height: 14),

          // 遥测参数小卡片网格
          Row(
            children: <Widget>[
              Expanded(
                child: Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: surfaceLowest,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: surfaceBorder),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      const Text(
                        'TRANSPORT MODE',
                        style: TextStyle(
                          color: textDim,
                          fontSize: 9,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        'QUIC 直连 :$listenPort',
                        style: const TextStyle(
                          color: textPrimary,
                          fontSize: 11,
                          fontWeight: FontWeight.w700,
                          fontFamily: 'monospace',
                        ),
                      ),
                      const SizedBox(height: 1),
                      const Text(
                        '双向对称多路复用',
                        style: TextStyle(color: textMuted, fontSize: 9),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: surfaceLowest,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: surfaceBorder),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      const Text(
                        'LATENCY & MTU',
                        style: TextStyle(
                          color: textDim,
                          fontSize: 9,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(height: 2),
                      const Text(
                        '2.4 ms (1280 字节)',
                        style: TextStyle(
                          color: textPrimary,
                          fontSize: 11,
                          fontWeight: FontWeight.w700,
                          fontFamily: 'monospace',
                        ),
                      ),
                      const SizedBox(height: 1),
                      const Text(
                        'PMTU 自适应探测',
                        style: TextStyle(color: textMuted, fontSize: 9),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),

          // 交互按钮：暂停传输 / 中止
          Row(
            children: <Widget>[
              Expanded(
                flex: 3,
                child: SizedBox(
                  height: 42,
                  child: ElevatedButton.icon(
                    onPressed: onTogglePause,
                    icon: Icon(
                      isTransferPaused ? Icons.play_arrow : Icons.pause,
                      size: 18,
                      color: bgAbyss,
                    ),
                    label: Text(
                      isTransferPaused ? '恢复传输' : '暂停传输',
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
              const SizedBox(width: 10),
              Expanded(
                flex: 2,
                child: SizedBox(
                  height: 42,
                  child: ElevatedButton.icon(
                    onPressed: onCancelTransfer,
                    icon: const Icon(Icons.close, size: 16, color: dangerCoral),
                    label: const Text(
                      '中止',
                      style: TextStyle(
                        color: dangerCoral,
                        fontSize: 13,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: dangerContainer,
                      elevation: 0,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(22),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildQueueAndHistorySection(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: <Widget>[
            Row(
              children: <Widget>[
                const Text(
                  '传输队列',
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(width: 8),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                  decoration: BoxDecoration(
                    color: surfaceContainer,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: surfaceBorder),
                  ),
                  child: Text(
                    '${receivedFiles.length} 已完成',
                    style: const TextStyle(color: textMuted, fontSize: 10),
                  ),
                ),
              ],
            ),
            GestureDetector(
              onTap: onClearHistory,
              child: const Text(
                '清空历史',
                style: TextStyle(
                  color: jade400,
                  fontSize: 12,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),

        if (receivedFiles.isEmpty)
          Container(
            padding: const EdgeInsets.symmetric(vertical: 24),
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: surfaceContainer,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: surfaceBorder),
            ),
            child: const Text(
              '队列空闲，暂无收发记录',
              style: TextStyle(color: textDim, fontSize: 12),
            ),
          )
        else
          ...receivedFiles.map((ReceivedFile file) {
            return Container(
              margin: const EdgeInsets.only(bottom: 8),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: surfaceContainer,
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: surfaceBorder),
              ),
              child: Row(
                children: <Widget>[
                  Container(
                    width: 36,
                    height: 36,
                    decoration: BoxDecoration(
                      color: surfaceActive,
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: borderLuminescent),
                    ),
                    child: const Icon(
                      Icons.insert_drive_file_outlined,
                      color: badgeRoute,
                      size: 18,
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Row(
                          children: <Widget>[
                            Flexible(
                              child: Text(
                                file.name,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: textPrimary,
                                  fontSize: 13,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ),
                            const SizedBox(width: 4),
                            const Icon(
                              Icons.verified,
                              color: jade400,
                              size: 14,
                            ),
                          ],
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
                            Text(
                              file.sender.isNotEmpty ? file.sender : '来自对端',
                              style: const TextStyle(color: textMuted, fontSize: 10),
                            ),
                          ],
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 8),
                  ElevatedButton(
                    onPressed: () => onOpenFile(file),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: surfaceActive,
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                      minimumSize: Size.zero,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                        side: const BorderSide(color: borderLuminescent),
                      ),
                      elevation: 0,
                    ),
                    child: const Text(
                      '打开',
                      style: TextStyle(
                        color: jade400,
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            );
          }),
      ],
    );
  }
}
