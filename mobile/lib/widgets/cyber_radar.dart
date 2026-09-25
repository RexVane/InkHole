import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../theme.dart';

/// 墨洞量子雷达与深渊引力核心 (Quantum InkHole Radar)
///
/// 1:1 还原 Cyber-Zen 视觉：
/// - 双层反向缓慢旋转的虚线轨道
/// - 顺时针雷达波束扫掠锥体 (Radar Sweeper Cone)
/// - 脉冲呼吸外环
/// - 中心墨洞深渊引力球 (径向深渊渐变 + 旋转吸积弧 + 点击选文件)
/// - 环绕子组件插槽 (用于动态放置被发现的设备节点)
class CyberRadar extends StatefulWidget {
  const CyberRadar({
    super.key,
    required this.transferPercent,
    required this.searching,
    this.orbitChildren = const <Widget>[],
    this.onTapCore,
  });

  /// -1 表示空闲待机；0..100 表示正在传输
  final double transferPercent;

  /// true 表示未锁定设备，雷达波纹与扫掠保持活跃
  final bool searching;

  /// 环绕在雷达四周的设备卡片
  final List<Widget> orbitChildren;

  /// 点击中心墨洞引力球
  final VoidCallback? onTapCore;

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
    // 雷达扫描锥面 5s 周期
    _sweepAnim = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 5),
    )..repeat();

    // 外轨道顺时针 24s
    _spinSlow = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 24),
    )..repeat();

    // 内轨道逆时针 32s
    _spinReverse = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 32),
    )..repeat();

    // 呼吸脉冲 4s
    _pulseRing = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 3600),
    )..repeat(reverse: true);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 应用退到后台时停掉 4 个常驻动画，避免持续耗电。
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
                    jade400.withValues(alpha: 0.12),
                    Colors.transparent,
                  ],
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
                  size: const Size.square(containerSize),
                  painter: _RadarBackgroundPainter(
                    sweepAngle: _sweepAnim.value * 2 * math.pi,
                    spinSlowAngle: _spinSlow.value * 2 * math.pi,
                    spinReverseAngle: -_spinReverse.value * 2 * math.pi,
                    pulseFactor: 0.94 + 0.08 * Curves.easeInOut.transform(_pulseRing.value),
                    pulseAlpha: 0.35 + 0.35 * _pulseRing.value,
                    searching: widget.searching,
                    transferPercent: widget.transferPercent,
                  ),
                );
              },
            ),

            // 3. 中心墨洞引力核心 (The Central Ink Void)
            GestureDetector(
              onTap: widget.onTapCore,
              behavior: HitTestBehavior.opaque,
              child: AnimatedScale(
                scale: 1.0,
                duration: const Duration(milliseconds: 150),
                child: Container(
                  width: coreSize,
                  height: coreSize,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: const RadialGradient(
                      center: Alignment.center,
                      radius: 0.9,
                      colors: <Color>[
                        Color(0xFF000000),
                        Color(0xFF020505),
                        Color(0xFF081819),
                        Color(0xFF143533),
                      ],
                      stops: <double>[0.0, 0.50, 0.80, 1.0],
                    ),
                    border: Border.all(
                      color: jade400.withValues(alpha: 0.35),
                      width: 1.2,
                    ),
                    boxShadow: <BoxShadow>[
                      BoxShadow(
                        color: jade400.withValues(alpha: 0.25),
                        blurRadius: 36,
                        spreadRadius: 2,
                      ),
                      const BoxShadow(
                        color: Color(0xFF010404),
                        blurRadius: 20,
                        spreadRadius: -4,
                      ),
                    ],
                  ),
                  child: Stack(
                    alignment: Alignment.center,
                    children: <Widget>[
                      // 旋转虚线吸积圆环
                      RotationTransition(
                        turns: _spinSlow,
                        child: CustomPaint(
                          size: const Size.square(coreSize - 20),
                          painter: _AccretionRingPainter(),
                        ),
                      ),

                      // 核心文字与图标
                      Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: <Widget>[
                          Container(
                            width: 34,
                            height: 34,
                            margin: const EdgeInsets.only(bottom: 4),
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              color: jade400.withValues(alpha: 0.12),
                            ),
                            child: Icon(
                              widget.transferPercent >= 0
                                  ? Icons.sync
                                  : Icons.add,
                              color: jade300,
                              size: 19,
                            ),
                          ),
                          Text(
                            widget.transferPercent >= 0
                                ? '${widget.transferPercent.toInt()}%'
                                : '投放文件',
                            style: const TextStyle(
                              color: textPrimary,
                              fontSize: 13,
                              fontWeight: FontWeight.w700,
                              letterSpacing: 1.2,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            widget.transferPercent >= 0
                                ? 'SYNCING DATA'
                                : 'READY TO SYNC',
                            style: TextStyle(
                              color: jade400.withValues(alpha: 0.75),
                              fontSize: 9,
                              fontFamily: 'monospace',
                              fontWeight: FontWeight.w600,
                              letterSpacing: 0.8,
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),

            // 4. 动态环绕设备节点
            ...widget.orbitChildren,
          ],
        ),
      ),
    );
  }
}

/// 绘制同心圆轨道、旋转雷达波束和呼吸脉冲光环
class _RadarBackgroundPainter extends CustomPainter {
  const _RadarBackgroundPainter({
    required this.sweepAngle,
    required this.spinSlowAngle,
    required this.spinReverseAngle,
    required this.pulseFactor,
    required this.pulseAlpha,
    required this.searching,
    required this.transferPercent,
  });

  final double sweepAngle;
  final double spinSlowAngle;
  final double spinReverseAngle;
  final double pulseFactor;
  final double pulseAlpha;
  final bool searching;
  final double transferPercent;

  @override
  void paint(Canvas canvas, Size size) {
    final Offset center = Offset(size.width / 2, size.height / 2);
    final double maxRadius = size.shortestSide / 2;

    // 1. 外轨道 1 (细虚线，缓慢顺时针)
    final Paint trackPaint1 = Paint()
      ..color = jade500.withValues(alpha: 0.18)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0;
    _drawDashedCircle(canvas, center, maxRadius * 0.96, trackPaint1, spinSlowAngle, 40, 8);

    // 2. 外轨道 2 (逆时针)
    final Paint trackPaint2 = Paint()
      ..color = jade400.withValues(alpha: 0.22)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.0;
    _drawDashedCircle(canvas, center, maxRadius * 0.82, trackPaint2, spinReverseAngle, 32, 6);

    // 3. 脉冲光环 (围绕核心呼吸)
    final double pulseRadius = (maxRadius * 0.72) * pulseFactor;
    final Paint pulsePaint = Paint()
      ..color = jade400.withValues(alpha: pulseAlpha * 0.45)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.8;
    canvas.drawCircle(center, pulseRadius, pulsePaint);

    // 4. 雷达扇面扫掠波束 (仅在 searching 或空闲时扫掠)
    if (searching || transferPercent < 0) {
      final Rect sweepRect = Rect.fromCircle(center: center, radius: maxRadius * 0.94);
      final Paint sweepPaint = Paint()
        ..shader = SweepGradient(
          startAngle: 0.0,
          endAngle: math.pi / 2.5,
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
      final double sweepRadian = (transferPercent.clamp(0, 100) / 100.0) * 2 * math.pi;
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
