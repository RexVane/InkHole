import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../theme.dart';
import 'iconsax.dart';

/// 墨洞量子引力雷达组件 (Cyber-Zen Radar & Singularity Core)
///
/// 在原版 Void + Jade 结构基础上融合 Iconsax 几何质感与精细同心刻度
class CyberRadar extends StatefulWidget {
  const CyberRadar({
    super.key,
    required this.transferPercent,
    required this.searching,
    required this.orbitChildren,
    required this.onTapCore,
  });

  /// 传输进度（0-100），小于 0 表示空闲
  final double transferPercent;

  /// 是否正在搜索局域网设备
  final bool searching;

  /// 环绕在轨道上的设备节点组件
  final List<Widget> orbitChildren;

  /// 点击中心黑洞触发操作（如选择文件）
  final VoidCallback onTapCore;

  @override
  State<CyberRadar> createState() => _CyberRadarState();
}

class _CyberRadarState extends State<CyberRadar>
    with TickerProviderStateMixin, WidgetsBindingObserver {
  late final AnimationController _sweepAnim;
  late final AnimationController _spinSlow;
  late final AnimationController _spinReverse;
  late final AnimationController _pulseRing;
  bool _ticking = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _sweepAnim = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 5),
    )..repeat();

    _spinSlow = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 24),
    )..repeat();

    _spinReverse = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 32),
    )..repeat();

    _pulseRing = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 3600),
    )..repeat(reverse: true);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final bool shouldTick = state == AppLifecycleState.resumed;
    if (shouldTick != _ticking) _setTicking(shouldTick);
  }

  void _setTicking(bool value) {
    _ticking = value;
    for (final AnimationController controller in <AnimationController>[
      _sweepAnim,
      _spinSlow,
      _spinReverse,
      _pulseRing,
    ]) {
      if (value) {
        if (!controller.isAnimating) {
          controller.repeat(reverse: identical(controller, _pulseRing));
        }
      } else if (controller.isAnimating) {
        controller.stop();
      }
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _sweepAnim.dispose();
    _spinSlow.dispose();
    _spinReverse.dispose();
    _pulseRing.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    const double containerSize = 320.0;
    const double coreSize = 168.0;

    return Center(
      child: SizedBox(
        width: containerSize,
        height: containerSize,
        child: Stack(
          alignment: Alignment.center,
          clipBehavior: Clip.none,
          children: <Widget>[
            // 1. 底层环境墨绿漫反射微光
            Container(
              width: 280,
              height: 280,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                gradient: RadialGradient(
                  colors: <Color>[
                    jade400.withValues(alpha: 0.13),
                    jade400.withValues(alpha: 0.03),
                    Colors.transparent,
                  ],
                  stops: const <double>[0.0, 0.55, 1.0],
                ),
              ),
            ),

            // 2. 动画雷达轨道与扫描锥体
            AnimatedBuilder(
              animation: Listenable.merge(<Listenable>[
                _sweepAnim,
                _spinSlow,
                _spinReverse,
                _pulseRing,
              ]),
              builder: (BuildContext context, Widget? child) {
                return CustomPaint(
                  size: const Size(containerSize, containerSize),
                  painter: _RadarBackgroundPainter(
                    sweepAngle: _sweepAnim.value * 2 * math.pi,
                    slowAngle: _spinSlow.value * 2 * math.pi,
                    reverseAngle: -_spinReverse.value * 2 * math.pi,
                    pulseFactor: _pulseRing.value,
                    searching: widget.searching,
                    transferPercent: widget.transferPercent,
                  ),
                );
              },
            ),

            // 3. 中心黑洞核心 (Singularity Event Horizon)
            GestureDetector(
              onTap: widget.onTapCore,
              behavior: HitTestBehavior.opaque,
              child: Container(
                width: coreSize,
                height: coreSize,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  gradient: const RadialGradient(
                    colors: <Color>[
                      Color(0xFF112622),
                      Color(0xFF0A1415),
                      void950,
                    ],
                    stops: <double>[0.0, 0.58, 1.0],
                  ),
                  border: Border.all(
                    color: jade400.withValues(alpha: 0.35),
                    width: 1.5,
                  ),
                  boxShadow: <BoxShadow>[
                    BoxShadow(
                      color: jade400.withValues(alpha: 0.18),
                      blurRadius: 36,
                      spreadRadius: 2,
                    ),
                    const BoxShadow(
                      color: Color(0xCC000000),
                      blurRadius: 24,
                      offset: Offset(0, 10),
                    ),
                  ],
                ),
                child: Stack(
                  alignment: Alignment.center,
                  children: <Widget>[
                    // 内部旋轉虚线圈
                    AnimatedBuilder(
                      animation: _spinSlow,
                      builder: (BuildContext context, Widget? child) {
                        return Transform.rotate(
                          angle: -_spinSlow.value * 2 * math.pi,
                          child: CustomPaint(
                            size: const Size(coreSize - 24, coreSize - 24),
                            painter: _AccretionRingPainter(),
                          ),
                        );
                      },
                    ),

                    // 中心文案与 Iconsax 图标
                    Column(
                      mainAxisSize: MainAxisSize.min,
                      children: <Widget>[
                        IconsaxBox(
                          icon: widget.transferPercent >= 0
                              ? Iconsax.transfer
                              : Iconsax.send,
                          size: 38,
                          iconSize: 20,
                          borderRadius: 11,
                          color: jade400,
                          backgroundColor: jade400.withValues(alpha: 0.12),
                          borderColor: jade400.withValues(alpha: 0.28),
                        ),
                        const SizedBox(height: 8),
                        Text(
                          widget.transferPercent >= 0
                              ? '${widget.transferPercent.toStringAsFixed(0)}%'
                              : '轻触选择文件',
                          style: TextStyle(
                            color: textPrimary,
                            fontSize: widget.transferPercent >= 0 ? 22 : 14,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 0.5,
                            fontFamily:
                                widget.transferPercent >= 0 ? 'monospace' : null,
                          ),
                        ),
                        const SizedBox(height: 3),
                        Text(
                          widget.transferPercent >= 0
                              ? '正在高速传输...'
                              : '先点周围设备，再发文件',
                          style: const TextStyle(
                            color: textMuted,
                            fontSize: 10,
                            letterSpacing: 0.3,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),

            // 4. 外部轨道上的设备节点
            ...widget.orbitChildren,
          ],
        ),
      ),
    );
  }
}

class _RadarBackgroundPainter extends CustomPainter {
  _RadarBackgroundPainter({
    required this.sweepAngle,
    required this.slowAngle,
    required this.reverseAngle,
    required this.pulseFactor,
    required this.searching,
    required this.transferPercent,
  });

  final double sweepAngle;
  final double slowAngle;
  final double reverseAngle;
  final double pulseFactor;
  final bool searching;
  final double transferPercent;

  @override
  void paint(Canvas canvas, Size size) {
    final Offset center = Offset(size.width / 2, size.height / 2);
    final double maxRadius = size.shortestSide / 2;

    // 0. 极细十字准星与四向锚点（Iconsax .line-dots 风格）
    final Paint crossPaint = Paint()
      ..color = jade400.withValues(alpha: 0.07)
      ..strokeWidth = 1.0;
    canvas.drawLine(
      Offset(center.dx - maxRadius * 0.96, center.dy),
      Offset(center.dx + maxRadius * 0.96, center.dy),
      crossPaint,
    );
    canvas.drawLine(
      Offset(center.dx, center.dy - maxRadius * 0.96),
      Offset(center.dx, center.dy + maxRadius * 0.96),
      crossPaint,
    );

    final Paint dotPaint = Paint()..color = jade400.withValues(alpha: 0.45);
    for (final Offset pt in <Offset>[
      Offset(center.dx - maxRadius * 0.94, center.dy),
      Offset(center.dx + maxRadius * 0.94, center.dy),
      Offset(center.dx, center.dy - maxRadius * 0.94),
      Offset(center.dx, center.dy + maxRadius * 0.94),
    ]) {
      canvas.drawCircle(pt, 2.0, dotPaint);
    }

    // 1. 最外层静态细环
    final Paint outerRingPaint = Paint()
      ..color = jade400.withValues(alpha: 0.14)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0;
    canvas.drawCircle(center, maxRadius * 0.94, outerRingPaint);

    // 2. 旋转虚线轨道 1 & 2
    final Paint dashedPaint1 = Paint()
      ..color = jade400.withValues(alpha: 0.20)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0;
    _drawDashedCircle(
      canvas,
      center,
      maxRadius * 0.78,
      dashedPaint1,
      slowAngle,
      36,
      5.0,
    );

    final Paint dashedPaint2 = Paint()
      ..color = secondaryTeal.withValues(alpha: 0.16)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0;
    _drawDashedCircle(
      canvas,
      center,
      maxRadius * 0.64,
      dashedPaint2,
      reverseAngle,
      28,
      6.0,
    );

    // 3. 呼吸扩散脉冲环
    final double pulseRadius =
        maxRadius * (0.54 + 0.38 * Curves.easeOutQuad.transform(pulseFactor));
    final double pulseAlpha =
        (1.0 - Curves.easeInQuad.transform(pulseFactor)).clamp(0.0, 1.0);
    final Paint pulsePaint = Paint()
      ..color = jade400.withValues(alpha: pulseAlpha * 0.42)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.6;
    canvas.drawCircle(center, pulseRadius, pulsePaint);

    // 4. 雷达扇面扫掠波束
    if (searching || transferPercent < 0) {
      final Rect sweepRect =
          Rect.fromCircle(center: center, radius: maxRadius * 0.94);
      final Paint sweepPaint = Paint()
        ..shader = SweepGradient(
          startAngle: 0.0,
          endAngle: math.pi / 2.4,
          colors: <Color>[
            jade400.withValues(alpha: 0.24),
            Colors.transparent,
          ],
          transform: GradientRotation(sweepAngle),
        ).createShader(sweepRect);

      canvas.drawCircle(center, maxRadius * 0.94, sweepPaint);
    }

    // 5. 传输状态进度弧
    if (transferPercent >= 0) {
      final double progressRadius = maxRadius * 0.62;
      final Paint bgRing = Paint()
        ..color = jade900.withValues(alpha: 0.5)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 4.0;
      canvas.drawCircle(center, progressRadius, bgRing);

      final Paint fgRing = Paint()
        ..color = jade400
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round
        ..strokeWidth = 4.0;
      final double sweepRadian =
          (transferPercent.clamp(0, 100) / 100.0) * 2 * math.pi;
      canvas.drawArc(
        Rect.fromCircle(center: center, radius: progressRadius),
        -math.pi / 2,
        sweepRadian,
        false,
        fgRing,
      );
    }
  }

  void _drawDashedCircle(
    Canvas canvas,
    Offset center,
    double radius,
    Paint paint,
    double startAngle,
    int dashCount,
    double dashLength,
  ) {
    final double step = (2 * math.pi) / dashCount;
    final double dashAngle = (dashLength / radius).clamp(0.01, step * 0.6);

    for (int i = 0; i < dashCount; i++) {
      final double angle = startAngle + i * step;
      canvas.drawArc(
        Rect.fromCircle(center: center, radius: radius),
        angle,
        dashAngle,
        false,
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _RadarBackgroundPainter old) {
    return old.sweepAngle != sweepAngle ||
        old.pulseFactor != pulseFactor ||
        old.searching != searching ||
        old.transferPercent != transferPercent;
  }
}

/// 绘制中心吸积盘内部的微型虚线圆环
class _AccretionRingPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final Offset center = Offset(size.width / 2, size.height / 2);
    final double radius = size.shortestSide / 2;

    final Paint p = Paint()
      ..color = jade400.withValues(alpha: 0.28)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0;

    const int dashes = 24;
    const double step = (2 * math.pi) / dashes;
    for (int i = 0; i < dashes; i++) {
      if (i % 2 == 0) {
        canvas.drawArc(
          Rect.fromCircle(center: center, radius: radius),
          i * step,
          step * 0.5,
          false,
          p,
        );
      }
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
