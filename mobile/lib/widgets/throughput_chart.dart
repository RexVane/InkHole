import 'package:flutter/material.dart';
import '../theme.dart';

/// 实时吞吐量波形图 (Throughput Sparkline Graph)
///
/// 1:1 对标设计图 5：
/// - 顶部峰值显示
/// - 平滑贝塞尔拟合曲线
/// - 渐变区域填充 (Surface to Jade)
/// - 实时动态末端脉冲发光节点
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
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: surfaceLowest,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: surfaceBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          // 顶部栏：标题与峰值。标题侧用 Expanded 兜底——峰值数字变宽时
          // 标题收缩省略，避免整行溢出（debug 包会画溢出警告横幅）。
          Row(
            children: <Widget>[
              Expanded(
                child: Row(
                  children: <Widget>[
                    Icon(Icons.speed, color: secondaryTeal, size: 16),
                    const SizedBox(width: 6),
                    const Flexible(
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
                  style: TextStyle(
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
    // 背景参考虚线
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

    // 如果数据不足，绘制一条平缓波纹
    if (speeds.length < 2) return;
    final List<double> data = speeds;

    final double maxVal = data.reduce((double a, double b) => a > b ? a : b).clamp(20.0, 1000.0);
    final double stepX = size.width / (data.length - 1);

    final Path linePath = Path();
    final Path fillPath = Path();

    final List<Offset> points = <Offset>[];
    for (int i = 0; i < data.length; i++) {
      final double x = i * stepX;
      final double y = size.height - (data[i] / maxVal) * (size.height * 0.82) - (size.height * 0.08);
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

    // 渐变填充
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

    // 翡翠绿平滑折线
    final Paint linePaint = Paint()
      ..color = jade400
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.0
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    canvas.drawPath(linePath, linePaint);

    // 末端动态脉冲圆点
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
  bool shouldRepaint(covariant _SparklinePainter oldDelegate) => true;
}
