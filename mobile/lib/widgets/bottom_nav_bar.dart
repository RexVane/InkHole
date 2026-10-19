import 'package:flutter/material.dart';
import '../theme.dart';
import 'iconsax.dart';

/// 全局底部导航栏 (5 大 Tab: 探索 · 传输 · 收件箱 · 配对 · 设置)
/// 融合 Iconsax `.menu-interfaz` 悬浮底栏与双色调矢量图标
class CyberBottomNavBar extends StatelessWidget {
  const CyberBottomNavBar({
    super.key,
    required this.currentIndex,
    required this.onTap,
  });

  final int currentIndex;
  final ValueChanged<int> onTap;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: surfaceContainer.withValues(alpha: 0.96),
        border: const Border(
          top: BorderSide(color: surfaceBorder, width: 1.0),
        ),
        boxShadow: const <BoxShadow>[
          BoxShadow(
            color: Color(0x99000000),
            blurRadius: 28,
            offset: Offset(0, -6),
          ),
        ],
      ),
      child: SafeArea(
        top: false,
        child: SizedBox(
          height: 62,
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceAround,
            children: <Widget>[
              _NavItem(
                index: 0,
                currentIndex: currentIndex,
                icon: Iconsax.radar,
                label: '探索',
                onTap: () => onTap(0),
              ),
              _NavItem(
                index: 1,
                currentIndex: currentIndex,
                icon: Iconsax.transfer,
                label: '传输',
                onTap: () => onTap(1),
              ),
              _NavItem(
                index: 2,
                currentIndex: currentIndex,
                icon: Iconsax.inbox,
                label: '收件箱',
                onTap: () => onTap(2),
              ),
              _NavItem(
                index: 3,
                currentIndex: currentIndex,
                icon: Iconsax.pairQr,
                label: '配对',
                onTap: () => onTap(3),
              ),
              _NavItem(
                index: 4,
                currentIndex: currentIndex,
                icon: Iconsax.settings,
                label: '设置',
                onTap: () => onTap(4),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _NavItem extends StatelessWidget {
  const _NavItem({
    required this.index,
    required this.currentIndex,
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final int index;
  final int currentIndex;
  final Iconsax icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final bool active = index == currentIndex;

    return Expanded(
      child: InkWell(
        onTap: onTap,
        splashColor: jade400.withValues(alpha: 0.08),
        highlightColor: Colors.transparent,
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            AnimatedScale(
              scale: active ? 1.08 : 1.0,
              duration: const Duration(milliseconds: 220),
              curve: Curves.easeOutCubic,
              child: IconsaxIcon(
                icon,
                size: 21,
                color: active ? jade400 : textMuted.withValues(alpha: 0.65),
                secondaryOpacity: active ? 0.38 : 0.22,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              label,
              style: TextStyle(
                color: active ? jade400 : textMuted,
                fontSize: 10,
                fontWeight: active ? FontWeight.w600 : FontWeight.w400,
                letterSpacing: 0.4,
              ),
            ),
            const SizedBox(height: 3),
            // Iconsax `.menu-interfaz:after` 底部胶囊指示条
            AnimatedContainer(
              duration: const Duration(milliseconds: 220),
              curve: Curves.easeOutCubic,
              width: active ? 22 : 0,
              height: 2.5,
              decoration: BoxDecoration(
                color: active ? jade400 : Colors.transparent,
                borderRadius: BorderRadius.circular(10),
                boxShadow: active
                    ? <BoxShadow>[
                        BoxShadow(
                          color: jade400.withValues(alpha: 0.5),
                          blurRadius: 6,
                        ),
                      ]
                    : null,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
