import 'package:flutter/material.dart';
import '../theme.dart';

/// 全局底部导航栏 (5 大 Tab: 雷达 · 收发 · 收件箱 · 配对 · 设置)
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
          top: BorderSide(color: surfaceBorder, width: 0.8),
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
          height: 60,
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceAround,
            children: <Widget>[
              _NavItem(
                index: 0,
                currentIndex: currentIndex,
                icon: Icons.radar,
                label: '雷达',
                onTap: () => onTap(0),
              ),
              _NavItem(
                index: 1,
                currentIndex: currentIndex,
                icon: Icons.swap_calls,
                label: '收发',
                onTap: () => onTap(1),
              ),
              _NavItem(
                index: 2,
                currentIndex: currentIndex,
                icon: Icons.folder_open,
                label: '收件箱',
                onTap: () => onTap(2),
              ),
              _NavItem(
                index: 3,
                currentIndex: currentIndex,
                icon: Icons.qr_code_scanner,
                label: '配对',
                onTap: () => onTap(3),
              ),
              _NavItem(
                index: 4,
                currentIndex: currentIndex,
                icon: Icons.tune,
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
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final bool active = index == currentIndex;

    return Expanded(
      child: InkWell(
        onTap: onTap,
        splashColor: jade400.withValues(alpha: 0.1),
        highlightColor: Colors.transparent,
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: <Widget>[
            Icon(
              icon,
              size: 22,
              color: active ? jade400 : textMuted,
            ),
            const SizedBox(height: 3),
            Text(
              label,
              style: TextStyle(
                color: active ? jade400 : textMuted,
                fontSize: 10,
                fontWeight: active ? FontWeight.w600 : FontWeight.w400,
                letterSpacing: 0.4,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
