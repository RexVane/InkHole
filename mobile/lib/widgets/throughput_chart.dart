import 'package:flutter/material.dart';
import '../theme.dart';
import 'iconsax.dart';

/// 实时吞吐量波形图 (Throughput Sparkline Graph)
///
/// 1:1 保持原版结构与文案，融合 Iconsax 双色调图标与细腻光晕
class ThroughputChart extends StatelessWidget {
  const ThroughputChart({
    super.key,
    required this.speeds,
    required this.currentSpeedMb,
    required this.peakSpeedMb,
  });

  /// 历史速度序列（MB/s）
  final List<double> speeds;
  final double currentSpeedMb;
  final double peakSpeedMb;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: surfaceLowest,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: surfaceBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          // 顶部栏：标题与峰值
          Row(
            children: <Widget>[
              const Expanded(
                child: Row(
                  children: <Widget>[
                    IconsaxIcon(
                      Iconsax.transfer,
                      color: secondaryTeal,
                      size: 16,
                    ),
                    SizedBox(width: 6),
                    Flexible(
                      child: Text(
                        'THROUGHPUT GRAPH (LAST 30S)',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: textMuted,
                          fontSize: 10,
                          fontWeight: FontWeight.w600,
                          letterSpacing: 0.5,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              Row(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  const Text(
                    '峰值  ',
                    style: TextStyle(color: textDim, fontSize: 10),
                  ),
                  Text(
                    '${peakSpeedMb.toStringAsFixed(0)} MB/s',
                    style: const TextStyle(
                      color: badgeRoute,
                      fontSize: 11,
                      fontFamily: 'monospace',
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ],
              ),
            ],
          ),
          const SizedBox(height: 10),

          // 核心波形图 Canvas
          SizedBox(
            height: 64,
            width: double.infinity,
            child: CustomPaint(
              painter: _SparklinePainter(speeds: speeds),
            ),
          ),
          const SizedBox(height: 8),

          // 底部速率与状态指示
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            crossAxisAlignment: CrossAxisAlignment.baseline,
            textBaseline: TextBaseline.alphabetic,
            children: <Widget>[
              Row(
                crossAxisAlignment: CrossAxisAlignment.baseline,
                textBaseline: TextBaseline.alphabetic,
                children: <Widget>[
                  Text(
                    currentSpeedMb.toStringAsFixed(1),
                    style: const TextStyle(
                      color: jade400,
                      fontSize: 22,
                      fontWeight: FontWeight.w800,
                      fontFamily: 'monospace',
                    ),
                  ),
                  const SizedBox(width: 4),
                  const Text(
                    'MB/s',
                    style: TextStyle(
                      color: jade400,
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      fontFamily: 'monospace',
                    ),
                  ),
                ],
              ),
              Flexible(
                child: Text(
                  speeds.isEmpty ? '等待传输数据' : '最近一分钟',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: badgeRoute,
                    fontSize: 10,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _SparklinePainter extends CustomPainter {
  const _SparklinePainter({required this.speeds});

  final List<double> speeds;

  @override
  void paint(Canvas canvas, Size size) {
    final Paint gridPaint = Paint()
      ..color = surfaceBorder
      ..style = PaintingStyle.stroke
      ..strokeWidth = 0.8;

    canvas.drawLine(
      Offset(0, size.height * 0.3),
      Offset(size.width, size.height * 0.3),
      gridPaint,
    );
    canvas.drawLine(
      Offset(0, size.height * 0.7),
      Offset(size.width, size.height * 0.7),
      gridPaint,
    );

    if (speeds.length < 2) return;
    final List<double> data = speeds;

    final double maxVal = data
        .reduce((double a, double b) => a > b ? a : b)
        .clamp(20.0, 1000.0);
    final double stepX = size.width / (data.length - 1);

    final Path linePath = Path();
    final Path fillPath = Path();

    final List<Offset> points = <Offset>[];
    for (int i = 0; i < data.length; i++) {
      final double x = i * stepX;
      final double y = size.height -
          (data[i] / maxVal) * (size.height * 0.82) -
          (size.height * 0.08);
      points.add(Offset(x, y.clamp(4.0, size.height - 4.0)));
    }

    linePath.moveTo(points[0].dx, points[0].dy);
    fillPath.moveTo(points[0].dx, size.height);
    fillPath.lineTo(points[0].dx, points[0].dy);

    for (int i = 0; i < points.length - 1; i++) {
      final Offset p0 = points[i];
      final Offset p1 = points[i + 1];
      final double controlX = (p0.dx + p1.dx) / 2;
      linePath.cubicTo(controlX, p0.dy, controlX, p1.dy, p1.dx, p1.dy);
      fillPath.cubicTo(controlX, p0.dy, controlX, p1.dy, p1.dx, p1.dy);
    }

    fillPath.lineTo(points.last.dx, size.height);
    fillPath.close();

    final Paint fillPaint = Paint()
      ..shader = LinearGradient(
        begin: Alignment.topCenter,
        end: Alignment.bottomCenter,
        colors: <Color>[
          jade400.withValues(alpha: 0.35),
          jade400.withValues(alpha: 0.0),
        ],
      ).createShader(Rect.fromLTWH(0, 0, size.width, size.height));
    canvas.drawPath(fillPath, fillPaint);

    final Paint linePaint = Paint()
      ..color = jade400
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.0
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    canvas.drawPath(linePath, linePaint);

    final Offset last = points.last;
    canvas.drawCircle(
      last,
      6.0,
      Paint()..color = jade400.withValues(alpha: 0.30),
    );
    canvas.drawCircle(
      last,
      3.0,
      Paint()..color = textPrimary,
    );
  }

  @override
  bool shouldRepaint(covariant _SparklinePainter oldDelegate) {
    return oldDelegate.speeds != speeds;
  }
}
