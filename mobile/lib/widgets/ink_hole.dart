import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../theme.dart';

/// 墨洞英雄区。
///
/// 一比一还原旧版 Android 的 InkHoleUI.kt#InkHoleHero：
/// 平时是墨黑核心 + 双层青弧缓慢反向旋转 + 呼吸；没发现设备时外圈播放雷达波纹；
/// 传输中外圈亮起青色进度环。轻点墨洞 = 选文件发送。
class InkHoleHero extends StatefulWidget {
  const InkHoleHero({
    super.key,
    required this.transferPercent,
    required this.searching,
    required this.diameter,
    this.onTap,
  });

  /// -1 = 空闲；0..100 = 显示进度环。
  final double transferPercent;

  /// true = 还没发现设备，播放雷达波纹。
  final bool searching;

  final double diameter;
  final VoidCallback? onTap;

  @override
  State<InkHoleHero> createState() => _InkHoleHeroState();
}

class _InkHoleHeroState extends State<InkHoleHero>
    with TickerProviderStateMixin {
  // 周期与旧版一致：内弧 46s 顺时针、外弧 71s 逆时针、呼吸 2.6s 往返、雷达 2.4s。
  late final AnimationController _spinInner;
  late final AnimationController _spinOuter;
  late final AnimationController _breath;
  late final AnimationController _radar;
  late final Listenable _ticks;

  @override
  void initState() {
    super.initState();
    _spinInner = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 46),
    )..repeat();
    _spinOuter = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 71),
    )..repeat();
    _breath = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2600),
    )..repeat(reverse: true);
    _radar = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2400),
    )..repeat();
    _ticks = Listenable.merge(<Listenable>[
      _spinInner,
      _spinOuter,
      _breath,
      _radar,
    ]);
  }

  @override
  void dispose() {
    _spinInner.dispose();
    _spinOuter.dispose();
    _breath.dispose();
    _radar.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final double target =
        widget.transferPercent >= 0 ? widget.transferPercent : 0.0;
    return Semantics(
      button: true,
      label: '轻点墨洞选择文件发送',
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: widget.onTap,
        child: TweenAnimationBuilder<double>(
          tween: Tween<double>(begin: 0, end: target),
          duration: const Duration(milliseconds: 240),
          builder: (BuildContext context, double percent, Widget? child) {
            return AnimatedBuilder(
              animation: _ticks,
              builder: (BuildContext context, Widget? inner) {
                return CustomPaint(
                  size: Size.square(widget.diameter),
                  painter: InkHolePainter(
                    spinInner: _spinInner.value * 360,
                    spinOuter: (1.0 - _spinOuter.value) * 360,
                    breath:
                        0.55 + 0.45 * Curves.easeInOut.transform(_breath.value),
                    radar: _radar.value,
                    searching: widget.searching,
                    showRing: widget.transferPercent >= 0,
                    percent: percent,
                  ),
                );
              },
            );
          },
        ),
      ),
    );
  }
}

/// 墨洞绘制。角度单位与 Compose 版保持一致（度），画的时候再转弧度。
class InkHolePainter extends CustomPainter {
  const InkHolePainter({
    required this.spinInner,
    required this.spinOuter,
    required this.breath,
    required this.radar,
    required this.searching,
    required this.showRing,
    required this.percent,
  });

  /// 内层吸积弧的旋转角（度，顺时针）。
  final double spinInner;

  /// 外层吸积弧的旋转角（度，逆时针）。
  final double spinOuter;

  /// 呼吸系数 0.55..1。
  final double breath;

  /// 雷达波纹进度 0..1。
  final double radar;

  final bool searching;
  final bool showRing;

  /// 0..100。
  final double percent;

  static const double _rad = math.pi / 180;

  @override
  void paint(Canvas canvas, Size size) {
    final Offset center = Offset(size.width / 2, size.height / 2);
    final double r = size.shortestSide / 2;

    // 雷达波纹：无设备时从洞口向外扩散。
    if (searching) {
      canvas.drawCircle(
        center,
        r * (0.62 + 0.36 * radar),
        Paint()
          ..color = inkTeal.withValues(alpha: (1.0 - radar) * 0.28)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2,
      );
    }

    // 墨洞主体：墨黑核心 -> 暗青过渡 -> 青色光晕 -> 透明。
    final double holeRadius = r * 0.94;
    canvas.drawCircle(
      center,
      holeRadius,
      Paint()
        ..shader = RadialGradient(
          colors: <Color>[
            const Color(0xFF000000),
            const Color(0xFF020807),
            const Color(0xFF0A2A25).withValues(alpha: 0.9),
            inkTeal.withValues(alpha: 0.40 * breath),
            const Color(0xFF1E5046).withValues(alpha: 0.15),
            Colors.transparent,
          ],
          stops: const <double>[0.0, 0.42, 0.60, 0.76, 0.90, 1.0],
        ).createShader(Rect.fromCircle(center: center, radius: holeRadius)),
    );

    // 吸积弧·内层（顺时针）——不对称弧才看得出旋转。
    final double innerRadius = r * 0.66;
    canvas.save();
    canvas.translate(center.dx, center.dy);
    canvas.rotate(spinInner * _rad);
    canvas.translate(-center.dx, -center.dy);
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: innerRadius),
      15 * _rad,
      105 * _rad,
      false,
      Paint()
        ..color = inkTealSoft.withValues(alpha: 0.30 * breath)
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round
        ..strokeWidth = 4,
    );
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: innerRadius * 0.86),
      195 * _rad,
      70 * _rad,
      false,
      Paint()
        ..color = inkTealSoft.withValues(alpha: 0.15 * breath)
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round
        ..strokeWidth = 2.5,
    );
    canvas.restore();

    // 吸积弧·外层（逆时针，更淡更慢）。
    canvas.save();
    canvas.translate(center.dx, center.dy);
    canvas.rotate(spinOuter * _rad);
    canvas.translate(-center.dx, -center.dy);
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: r * 0.84),
      60 * _rad,
      140 * _rad,
      false,
      Paint()
        ..color = inkTealSoft.withValues(alpha: 0.12)
        ..style = PaintingStyle.stroke
        ..strokeCap = StrokeCap.round
        ..strokeWidth = 2,
    );
    canvas.restore();

    // 传输进度环。
    if (showRing) {
      final double ringRadius = r * 0.97;
      canvas.drawCircle(
        center,
        ringRadius,
        Paint()
          ..color = inkTealDim.withValues(alpha: 0.5)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 5,
      );
      canvas.drawArc(
        Rect.fromCircle(center: center, radius: ringRadius),
        -90 * _rad,
        3.6 * percent.clamp(0, 100).toDouble() * _rad,
        false,
        Paint()
          ..color = inkTeal
          ..style = PaintingStyle.stroke
          ..strokeCap = StrokeCap.round
          ..strokeWidth = 5,
      );
    }
  }

  @override
  bool shouldRepaint(covariant InkHolePainter oldDelegate) {
    return oldDelegate.spinInner != spinInner ||
        oldDelegate.spinOuter != spinOuter ||
        oldDelegate.breath != breath ||
        oldDelegate.radar != radar ||
        oldDelegate.searching != searching ||
        oldDelegate.showRing != showRing ||
        oldDelegate.percent != percent;
  }
}
