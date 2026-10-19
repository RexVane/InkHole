import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../theme.dart';

/// Iconsax (https://iconsax.io/) 风格双色调 (Bulk / Twotone) 矢量图标枚举
enum Iconsax {
  radar,
  transfer,
  inbox,
  pairQr,
  settings,
  send,
  key,
  scanner,
  refresh,
  arrowLeft,
  copy,
  check,
  folder,
  terminal,
  shield,
  flash,
  flashOff,
  gallery,
  trash,
  pause,
  play,
  closeCircle,
  cloud,
  server,
  route,
  lock,
  unlock,
  link,
  update,
  edit,
  tune,
  fingerprint,
  layers,
  hub,
  wifiTethering,
  openExternal,
  deviceLaptop,
  devicePhone,
  deviceTablet,
  fileDoc,
  fileMedia,
  fileCode,
  fileArchive,
  fileApk,
  filePdf,
  fileGeneric,
}

/// Iconsax 双色调 (Bulk & Twotone) 矢量图标组件
///
/// 采用 Iconsax 标志性的 24x24 几何规范：
/// - 副路径（超椭圆/背景块）使用 `0.36` 透明度柔和填充或描边
/// - 主路径使用 `1.0` 透明度与 `StrokeCap.round` 圆角几何线条
class IconsaxIcon extends StatelessWidget {
  const IconsaxIcon(
    this.icon, {
    super.key,
    this.size = 20,
    this.color = jade400,
    this.secondaryOpacity = 0.36,
  });

  final Iconsax icon;
  final double size;
  final Color color;
  final double secondaryOpacity;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: size,
      height: size,
      child: CustomPaint(
        painter: _IconsaxPainter(
          icon: icon,
          color: color,
          secondaryOpacity: secondaryOpacity,
        ),
      ),
    );
  }
}

/// Iconsax `.con-svg` 超椭圆图标容器 (Squircle Icon Container)
class IconsaxBox extends StatelessWidget {
  const IconsaxBox({
    super.key,
    required this.icon,
    this.size = 40,
    this.iconSize = 20,
    this.color = jade400,
    this.backgroundColor,
    this.borderColor,
    this.borderRadius = 12,
  });

  final Iconsax icon;
  final double size;
  final double iconSize;
  final Color color;
  final Color? backgroundColor;
  final Color? borderColor;
  final double borderRadius;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: ShapeDecoration(
        color: backgroundColor ?? color.withValues(alpha: 0.10),
        shape: ContinuousRectangleBorder(
          borderRadius: BorderRadius.circular(borderRadius * 2.2),
          side: BorderSide(
            color: borderColor ?? color.withValues(alpha: 0.22),
            width: 1.0,
          ),
        ),
      ),
      alignment: Alignment.center,
      child: IconsaxIcon(
        icon,
        size: iconSize,
        color: color,
      ),
    );
  }
}

/// Iconsax `.line-dots` 结构极细分割线（两端带 4px 发光锚点）
class IconsaxLineDots extends StatelessWidget {
  const IconsaxLineDots({
    super.key,
    this.dotColor = jade400,
    this.lineColor = surfaceBorder,
    this.margin = EdgeInsets.zero,
  });

  final Color dotColor;
  final Color lineColor;
  final EdgeInsetsGeometry margin;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: margin,
      child: SizedBox(
        height: 5,
        child: Row(
          children: <Widget>[
            Container(
              width: 4,
              height: 4,
              decoration: BoxDecoration(
                color: dotColor.withValues(alpha: 0.75),
                shape: BoxShape.circle,
              ),
            ),
            Expanded(
              child: Container(
                height: 1,
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    colors: <Color>[
                      dotColor.withValues(alpha: 0.35),
                      lineColor,
                      dotColor.withValues(alpha: 0.35),
                    ],
                  ),
                ),
              ),
            ),
            Container(
              width: 4,
              height: 4,
              decoration: BoxDecoration(
                color: dotColor.withValues(alpha: 0.75),
                shape: BoxShape.circle,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Iconsax `.circle-progress` 环形进度指示器
class IconsaxRingProgress extends StatelessWidget {
  const IconsaxRingProgress({
    super.key,
    required this.fraction,
    this.size = 46,
    this.color = jade400,
    this.label,
  });

  final double fraction;
  final double size;
  final Color color;
  final String? label;

  @override
  Widget build(BuildContext context) {
    final double clamped = fraction.clamp(0.0, 1.0);
    final int pct = (clamped * 100).round();
    return SizedBox(
      width: size,
      height: size,
      child: Stack(
        alignment: Alignment.center,
        children: <Widget>[
          CustomPaint(
            size: Size.square(size),
            painter: _RingProgressPainter(
              fraction: clamped,
              color: color,
            ),
          ),
          Text(
            label ?? '$pct%',
            style: TextStyle(
              color: textPrimary,
              fontSize: size * 0.23,
              fontWeight: FontWeight.w700,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }
}

class _RingProgressPainter extends CustomPainter {
  const _RingProgressPainter({
    required this.fraction,
    required this.color,
  });

  final double fraction;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final Offset center = Offset(size.width / 2, size.height / 2);
    final double radius = (size.shortestSide / 2) - 4;

    final Paint bgPaint = Paint()
      ..color = textPrimary.withValues(alpha: 0.12)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.2;
    canvas.drawCircle(center, radius, bgPaint);

    if (fraction > 0) {
      final Paint fgPaint = Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.4
        ..strokeCap = StrokeCap.round;
      canvas.drawArc(
        Rect.fromCircle(center: center, radius: radius),
        -math.pi / 2,
        fraction * 2 * math.pi,
        false,
        fgPaint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _RingProgressPainter oldDelegate) {
    return oldDelegate.fraction != fraction || oldDelegate.color != color;
  }
}

class _IconsaxPainter extends CustomPainter {
  const _IconsaxPainter({
    required this.icon,
    required this.color,
    required this.secondaryOpacity,
  });

  final Iconsax icon;
  final Color color;
  final double secondaryOpacity;

  /// 绘制 Iconsax 标准 24x24 超椭圆底块 (Squircle Bulk Base)
  void _drawSquircleBulk(Canvas canvas, Paint fill, {double inset = 2.5}) {
    final RRect rr = RRect.fromRectAndRadius(
      Rect.fromLTRB(inset, inset, 24 - inset, 24 - inset),
      Radius.circular((24 - inset * 2) * 0.34),
    );
    canvas.drawRRect(rr, fill);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final double scale = size.shortestSide / 24.0;
    canvas.save();
    canvas.scale(scale, scale);

    final Color secColor = color.withValues(alpha: secondaryOpacity);
    final Paint bulkFill = Paint()
      ..color = secColor
      ..style = PaintingStyle.fill;
    final Paint primaryFill = Paint()
      ..color = color
      ..style = PaintingStyle.fill;
    final Paint secStroke = Paint()
      ..color = secColor
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.75
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final Paint stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.75
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;

    switch (icon) {
      case Iconsax.radar:
        canvas.drawCircle(const Offset(12, 12), 9.5, bulkFill);
        canvas.drawCircle(const Offset(12, 12), 9.5, secStroke);
        canvas.drawCircle(const Offset(12, 12), 5.5, secStroke);
        canvas.drawArc(
          Rect.fromCircle(center: const Offset(12, 12), radius: 9.5),
          -math.pi * 0.75,
          math.pi * 0.65,
          false,
          stroke,
        );
        canvas.drawLine(const Offset(12, 12), const Offset(18.2, 5.8), stroke);
        canvas.drawCircle(const Offset(12, 12), 2.2, primaryFill);

      case Iconsax.transfer:
        _drawSquircleBulk(canvas, bulkFill);
        final Path p1 = Path()
          ..moveTo(7.5, 10.0)
          ..lineTo(16.5, 10.0)
          ..lineTo(13.8, 7.3);
        final Path p2 = Path()
          ..moveTo(16.5, 14.0)
          ..lineTo(7.5, 14.0)
          ..lineTo(10.2, 16.7);
        canvas.drawPath(p1, stroke);
        canvas.drawPath(p2, stroke);

      case Iconsax.inbox:
        final Path folderBack = Path()
          ..moveTo(3, 7.5)
          ..quadraticBezierTo(3, 4.5, 6, 4.5)
          ..lineTo(9.2, 4.5)
          ..lineTo(11.2, 6.8)
          ..lineTo(18, 6.8)
          ..quadraticBezierTo(21, 6.8, 21, 9.8)
          ..lineTo(21, 17.5)
          ..quadraticBezierTo(21, 20, 18, 20)
          ..lineTo(6, 20)
          ..quadraticBezierTo(3, 20, 3, 17.5)
          ..close();
        canvas.drawPath(folderBack, bulkFill);
        final Path tray = Path()
          ..moveTo(3.2, 13.5)
          ..lineTo(8.2, 13.5)
          ..quadraticBezierTo(9.5, 16.2, 12, 16.2)
          ..quadraticBezierTo(14.5, 16.2, 15.8, 13.5)
          ..lineTo(20.8, 13.5);
        canvas.drawPath(tray, stroke);
        canvas.drawPath(folderBack, secStroke);

      case Iconsax.pairQr:
      case Iconsax.scanner:
        // 四角圆润取景框
        final Path corners = Path()
          ..moveTo(3, 8.5)
          ..lineTo(3, 6.2)
          ..quadraticBezierTo(3, 3, 6.2, 3)
          ..lineTo(8.5, 3)
          ..moveTo(15.5, 3)
          ..lineTo(17.8, 3)
          ..quadraticBezierTo(21, 3, 21, 6.2)
          ..lineTo(21, 8.5)
          ..moveTo(21, 15.5)
          ..lineTo(21, 17.8)
          ..quadraticBezierTo(21, 21, 17.8, 21)
          ..lineTo(15.5, 21)
          ..moveTo(8.5, 21)
          ..lineTo(6.2, 21)
          ..quadraticBezierTo(3, 21, 3, 17.8)
          ..lineTo(3, 15.5);
        canvas.drawPath(corners, stroke);
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(6.5, 6.5, 4.2, 4.2),
            const Radius.circular(1.3),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(13.3, 6.5, 4.2, 4.2),
            const Radius.circular(1.3),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(6.5, 13.3, 4.2, 4.2),
            const Radius.circular(1.3),
          ),
          bulkFill,
        );
        canvas.drawLine(const Offset(5.5, 12), const Offset(18.5, 12), stroke);

      case Iconsax.settings:
        canvas.drawCircle(const Offset(12, 12), 8.8, bulkFill);
        canvas.drawCircle(const Offset(12, 12), 3.2, stroke);
        for (int i = 0; i < 6; i++) {
          final double a = i * (math.pi / 3);
          canvas.drawLine(
            Offset(12 + math.cos(a) * 6.5, 12 + math.sin(a) * 6.5),
            Offset(12 + math.cos(a) * 9.2, 12 + math.sin(a) * 9.2),
            stroke,
          );
        }

      case Iconsax.send:
        final Path plane = Path()
          ..moveTo(7.4, 6.3)
          ..lineTo(16.9, 3.1)
          ..quadraticBezierTo(21.2, 1.7, 19.8, 6.0)
          ..lineTo(16.6, 15.5)
          ..quadraticBezierTo(14.5, 21.8, 12.2, 16.5)
          ..lineTo(10.8, 13.1)
          ..lineTo(7.4, 11.7)
          ..quadraticBezierTo(2.1, 9.4, 7.4, 6.3)
          ..close();
        canvas.drawPath(plane, bulkFill);
        canvas.drawPath(plane, stroke);
        canvas.drawLine(const Offset(10.9, 13.0), const Offset(15.2, 8.7), stroke);

      case Iconsax.key:
        canvas.drawCircle(const Offset(9.2, 14.8), 5.2, bulkFill);
        canvas.drawCircle(const Offset(9.2, 14.8), 5.2, stroke);
        final Path blade = Path()
          ..moveTo(13.0, 11.0)
          ..lineTo(19.5, 4.5)
          ..lineTo(21.0, 6.0)
          ..moveTo(16.8, 7.2)
          ..lineTo(18.8, 9.2);
        canvas.drawPath(blade, stroke);
        canvas.drawCircle(const Offset(8.5, 15.5), 1.2, primaryFill);

      case Iconsax.refresh:
        canvas.drawCircle(const Offset(12, 12), 8.5, bulkFill);
        canvas.drawArc(
          Rect.fromCircle(center: const Offset(12, 12), radius: 8.2),
          -math.pi * 0.8,
          math.pi * 1.45,
          false,
          stroke,
        );
        final Path arrow = Path()
          ..moveTo(16.2, 4.5)
          ..lineTo(19.2, 6.8)
          ..lineTo(15.6, 8.6);
        canvas.drawPath(arrow, stroke);

      case Iconsax.arrowLeft:
        canvas.drawLine(const Offset(19, 12), const Offset(5.5, 12), secStroke);
        final Path chevron = Path()
          ..moveTo(11.5, 6.0)
          ..lineTo(5.5, 12.0)
          ..lineTo(11.5, 18.0);
        canvas.drawPath(chevron, stroke);

      case Iconsax.copy:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(8.5, 3.5, 12, 12),
            const Radius.circular(3.5),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(8.5, 3.5, 12, 12),
            const Radius.circular(3.5),
          ),
          secStroke,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(3.5, 8.5, 12, 12),
            const Radius.circular(3.5),
          ),
          stroke,
        );

      case Iconsax.check:
        canvas.drawCircle(const Offset(12, 12), 9.2, bulkFill);
        canvas.drawCircle(const Offset(12, 12), 9.2, secStroke);
        final Path tick = Path()
          ..moveTo(8.0, 12.2)
          ..lineTo(10.8, 15.0)
          ..lineTo(16.2, 9.5);
        canvas.drawPath(tick, stroke);

      case Iconsax.folder:
        final Path f = Path()
          ..moveTo(3, 8)
          ..quadraticBezierTo(3, 5, 6, 5)
          ..lineTo(9.5, 5)
          ..lineTo(11.8, 7.5)
          ..lineTo(18, 7.5)
          ..quadraticBezierTo(21, 7.5, 21, 10.5)
          ..lineTo(21, 17)
          ..quadraticBezierTo(21, 20, 18, 20)
          ..lineTo(6, 20)
          ..quadraticBezierTo(3, 20, 3, 17)
          ..close();
        canvas.drawPath(f, bulkFill);
        canvas.drawPath(f, stroke);
        canvas.drawLine(const Offset(8, 13.8), const Offset(16, 13.8), stroke);

      case Iconsax.terminal:
        _drawSquircleBulk(canvas, bulkFill);
        _drawSquircleBulk(canvas, secStroke);
        final Path prompt = Path()
          ..moveTo(7.5, 9.0)
          ..lineTo(10.8, 12.0)
          ..lineTo(7.5, 15.0);
        canvas.drawPath(prompt, stroke);
        canvas.drawLine(const Offset(12.8, 15.0), const Offset(16.5, 15.0), stroke);

      case Iconsax.shield:
        final Path sh = Path()
          ..moveTo(12, 2.8)
          ..lineTo(19.2, 5.6)
          ..lineTo(19.2, 11.8)
          ..quadraticBezierTo(19.2, 17.8, 12, 21.2)
          ..quadraticBezierTo(4.8, 17.8, 4.8, 11.8)
          ..lineTo(4.8, 5.6)
          ..close();
        canvas.drawPath(sh, bulkFill);
        canvas.drawPath(sh, stroke);
        final Path tick = Path()
          ..moveTo(9.0, 12.0)
          ..lineTo(11.1, 14.1)
          ..lineTo(15.2, 10.0);
        canvas.drawPath(tick, stroke);

      case Iconsax.flash:
      case Iconsax.flashOff:
        final Path bolt = Path()
          ..moveTo(13.2, 2.8)
          ..lineTo(5.5, 13.2)
          ..lineTo(11.5, 13.2)
          ..lineTo(10.8, 21.2)
          ..lineTo(18.5, 10.8)
          ..lineTo(12.5, 10.8)
          ..close();
        canvas.drawPath(bolt, bulkFill);
        canvas.drawPath(bolt, stroke);
        if (icon == Iconsax.flashOff) {
          canvas.drawLine(const Offset(4, 4), const Offset(20, 20), stroke);
        }

      case Iconsax.gallery:
      case Iconsax.fileMedia:
        _drawSquircleBulk(canvas, bulkFill);
        _drawSquircleBulk(canvas, secStroke);
        canvas.drawCircle(const Offset(9.0, 8.8), 1.8, primaryFill);
        final Path wave = Path()
          ..moveTo(3.5, 17.8)
          ..lineTo(8.5, 13.5)
          ..quadraticBezierTo(10.0, 12.4, 11.5, 13.8)
          ..lineTo(13.5, 15.5)
          ..quadraticBezierTo(15.0, 16.8, 16.5, 15.2)
          ..lineTo(20.5, 11.5);
        canvas.drawPath(wave, stroke);

      case Iconsax.trash:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(6, 7, 12, 13.5),
            const Radius.circular(3),
          ),
          bulkFill,
        );
        canvas.drawLine(const Offset(4.5, 6.5), const Offset(19.5, 6.5), stroke);
        canvas.drawLine(const Offset(9.5, 4.0), const Offset(14.5, 4.0), stroke);
        canvas.drawLine(const Offset(10, 11), const Offset(10, 16.5), stroke);
        canvas.drawLine(const Offset(14, 11), const Offset(14, 16.5), stroke);

      case Iconsax.pause:
        canvas.drawCircle(const Offset(12, 12), 9.2, bulkFill);
        canvas.drawLine(const Offset(9.8, 8.8), const Offset(9.8, 15.2), stroke);
        canvas.drawLine(const Offset(14.2, 8.8), const Offset(14.2, 15.2), stroke);

      case Iconsax.play:
        canvas.drawCircle(const Offset(12, 12), 9.2, bulkFill);
        final Path tri = Path()
          ..moveTo(10.0, 8.4)
          ..lineTo(16.0, 12.0)
          ..lineTo(10.0, 15.6)
          ..close();
        canvas.drawPath(tri, primaryFill);

      case Iconsax.closeCircle:
        canvas.drawCircle(const Offset(12, 12), 9.2, bulkFill);
        canvas.drawLine(const Offset(9.2, 9.2), const Offset(14.8, 14.8), stroke);
        canvas.drawLine(const Offset(14.8, 9.2), const Offset(9.2, 14.8), stroke);

      case Iconsax.cloud:
        final Path c = Path()
          ..moveTo(7.2, 18.2)
          ..lineTo(16.8, 18.2)
          ..quadraticBezierTo(20.5, 18.2, 20.5, 14.5)
          ..quadraticBezierTo(20.5, 11.2, 17.4, 10.8)
          ..quadraticBezierTo(16.2, 6.0, 11.5, 6.0)
          ..quadraticBezierTo(7.2, 6.0, 6.4, 10.2)
          ..quadraticBezierTo(3.5, 10.8, 3.5, 14.2)
          ..quadraticBezierTo(3.5, 18.2, 7.2, 18.2);
        canvas.drawPath(c, bulkFill);
        canvas.drawPath(c, stroke);

      case Iconsax.server:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(3.5, 4, 17, 6.5),
            const Radius.circular(2.5),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(3.5, 4, 17, 6.5),
            const Radius.circular(2.5),
          ),
          stroke,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(3.5, 13.5, 17, 6.5),
            const Radius.circular(2.5),
          ),
          stroke,
        );
        canvas.drawCircle(const Offset(7.2, 7.2), 1.1, primaryFill);
        canvas.drawCircle(const Offset(7.2, 16.8), 1.1, primaryFill);

      case Iconsax.route:
      case Iconsax.hub:
        canvas.drawCircle(const Offset(6, 6), 2.8, bulkFill);
        canvas.drawCircle(const Offset(18, 6), 2.8, bulkFill);
        canvas.drawCircle(const Offset(12, 18), 3.0, primaryFill);
        canvas.drawCircle(const Offset(6, 6), 2.8, stroke);
        canvas.drawCircle(const Offset(18, 6), 2.8, stroke);
        canvas.drawLine(const Offset(8.2, 7.8), const Offset(11.0, 15.5), stroke);
        canvas.drawLine(const Offset(15.8, 7.8), const Offset(13.0, 15.5), stroke);

      case Iconsax.lock:
      case Iconsax.unlock:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(4.5, 10.5, 15, 10),
            const Radius.circular(4),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(4.5, 10.5, 15, 10),
            const Radius.circular(4),
          ),
          stroke,
        );
        canvas.drawArc(
          const Rect.fromLTWH(8, 4.2, 8, 9),
          math.pi,
          icon == Iconsax.lock ? math.pi : math.pi * 0.72,
          false,
          stroke,
        );
        canvas.drawCircle(const Offset(12, 15.5), 1.4, primaryFill);

      case Iconsax.link:
        canvas.drawArc(
          const Rect.fromLTWH(3.5, 7.5, 9, 9),
          math.pi * 0.5,
          math.pi,
          false,
          stroke,
        );
        canvas.drawArc(
          const Rect.fromLTWH(11.5, 7.5, 9, 9),
          -math.pi * 0.5,
          math.pi,
          false,
          secStroke,
        );
        canvas.drawLine(const Offset(8.5, 12), const Offset(15.5, 12), stroke);

      case Iconsax.update:
        _drawSquircleBulk(canvas, bulkFill);
        canvas.drawLine(const Offset(12, 6.5), const Offset(12, 15.2), stroke);
        final Path down = Path()
          ..moveTo(8.5, 12.2)
          ..lineTo(12, 15.8)
          ..lineTo(15.5, 12.2);
        canvas.drawPath(down, stroke);
        canvas.drawLine(const Offset(7.5, 18.2), const Offset(16.5, 18.2), stroke);

      case Iconsax.edit:
        final Path pen = Path()
          ..moveTo(14.5, 4.8)
          ..lineTo(19.2, 9.5)
          ..lineTo(8.8, 19.8)
          ..lineTo(4.2, 20.2)
          ..lineTo(4.6, 15.6)
          ..close();
        canvas.drawPath(pen, bulkFill);
        canvas.drawPath(pen, stroke);

      case Iconsax.tune:
        _drawSquircleBulk(canvas, bulkFill);
        canvas.drawLine(const Offset(7.5, 7), const Offset(7.5, 17), secStroke);
        canvas.drawLine(const Offset(12, 7), const Offset(12, 17), secStroke);
        canvas.drawLine(const Offset(16.5, 7), const Offset(16.5, 17), secStroke);
        canvas.drawCircle(const Offset(7.5, 10), 2.0, primaryFill);
        canvas.drawCircle(const Offset(12, 14.5), 2.0, primaryFill);
        canvas.drawCircle(const Offset(16.5, 9.5), 2.0, primaryFill);

      case Iconsax.fingerprint:
        canvas.drawCircle(const Offset(12, 12), 9.0, bulkFill);
        canvas.drawArc(
          Rect.fromCircle(center: const Offset(12, 12), radius: 7.2),
          -math.pi * 0.85,
          math.pi * 1.5,
          false,
          secStroke,
        );
        canvas.drawArc(
          Rect.fromCircle(center: const Offset(12, 12), radius: 4.4),
          -math.pi * 0.75,
          math.pi * 1.5,
          false,
          stroke,
        );
        canvas.drawLine(const Offset(12, 10.5), const Offset(12, 15.5), stroke);

      case Iconsax.layers:
        final Path topLayer = Path()
          ..moveTo(12, 4.2)
          ..lineTo(20, 8.5)
          ..lineTo(12, 12.8)
          ..lineTo(4, 8.5)
          ..close();
        canvas.drawPath(topLayer, bulkFill);
        canvas.drawPath(topLayer, stroke);
        final Path midLayer = Path()
          ..moveTo(4, 12.8)
          ..lineTo(12, 17.0)
          ..lineTo(20, 12.8);
        final Path botLayer = Path()
          ..moveTo(4, 16.5)
          ..lineTo(12, 20.5)
          ..lineTo(20, 16.5);
        canvas.drawPath(midLayer, secStroke);
        canvas.drawPath(botLayer, stroke);

      case Iconsax.wifiTethering:
        canvas.drawCircle(const Offset(12, 12), 9.0, bulkFill);
        canvas.drawArc(
          Rect.fromCircle(center: const Offset(12, 13.5), radius: 7.5),
          -math.pi * 0.8,
          math.pi * 0.6,
          false,
          secStroke,
        );
        canvas.drawArc(
          Rect.fromCircle(center: const Offset(12, 13.5), radius: 4.5),
          -math.pi * 0.8,
          math.pi * 0.6,
          false,
          stroke,
        );
        canvas.drawCircle(const Offset(12, 15), 1.8, primaryFill);

      case Iconsax.openExternal:
        _drawSquircleBulk(canvas, bulkFill, inset: 3.5);
        final Path arr = Path()
          ..moveTo(13, 5.5)
          ..lineTo(18.5, 5.5)
          ..lineTo(18.5, 11)
          ..moveTo(18.2, 5.8)
          ..lineTo(9.5, 14.5);
        canvas.drawPath(arr, stroke);

      case Iconsax.deviceLaptop:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(4.5, 5, 15, 10.5),
            const Radius.circular(2.5),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(4.5, 5, 15, 10.5),
            const Radius.circular(2.5),
          ),
          stroke,
        );
        canvas.drawLine(const Offset(2.5, 18.5), const Offset(21.5, 18.5), stroke);

      case Iconsax.devicePhone:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(6.5, 3, 11, 18),
            const Radius.circular(3.2),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(6.5, 3, 11, 18),
            const Radius.circular(3.2),
          ),
          stroke,
        );
        canvas.drawCircle(const Offset(12, 17.5), 1.1, primaryFill);

      case Iconsax.deviceTablet:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(4.5, 3, 15, 18),
            const Radius.circular(3.2),
          ),
          bulkFill,
        );
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(4.5, 3, 15, 18),
            const Radius.circular(3.2),
          ),
          stroke,
        );
        canvas.drawCircle(const Offset(12, 17.5), 1.1, primaryFill);

      case Iconsax.fileDoc:
      case Iconsax.filePdf:
      case Iconsax.fileGeneric:
        _drawSquircleBulk(canvas, bulkFill);
        _drawSquircleBulk(canvas, secStroke);
        canvas.drawLine(const Offset(8, 9), const Offset(13, 9), stroke);
        canvas.drawLine(const Offset(8, 13), const Offset(16, 13), stroke);
        canvas.drawLine(const Offset(8, 17), const Offset(14, 17), stroke);

      case Iconsax.fileCode:
        _drawSquircleBulk(canvas, bulkFill);
        _drawSquircleBulk(canvas, secStroke);
        final Path code = Path()
          ..moveTo(9.5, 9.5)
          ..lineTo(7.0, 12.0)
          ..lineTo(9.5, 14.5)
          ..moveTo(14.5, 9.5)
          ..lineTo(17.0, 12.0)
          ..lineTo(14.5, 14.5);
        canvas.drawPath(code, stroke);

      case Iconsax.fileArchive:
      case Iconsax.fileApk:
        _drawSquircleBulk(canvas, bulkFill);
        _drawSquircleBulk(canvas, stroke);
        canvas.drawLine(const Offset(12, 4), const Offset(12, 14), stroke);
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTWH(10, 13, 4, 4.5),
            const Radius.circular(1.2),
          ),
          primaryFill,
        );
    }

    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _IconsaxPainter oldDelegate) {
    return oldDelegate.icon != icon ||
        oldDelegate.color != color ||
        oldDelegate.secondaryOpacity != secondaryOpacity;
  }
}
