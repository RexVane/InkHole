import 'package:flutter/material.dart';

import '../models.dart';
import '../theme.dart';

/// 设备胶囊，对应旧版 InkHoleUI.kt#DeviceChip：
/// 22dp 圆角、选中时底色/描边变青并在末尾点亮一颗 7dp 圆点。
class DeviceChip extends StatelessWidget {
  const DeviceChip({
    super.key,
    required this.peer,
    required this.selected,
    required this.onTap,
  });

  final PeerView peer;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 180),
      decoration: BoxDecoration(
        color: selected ? inkBgCardActive : inkBgCard,
        borderRadius: BorderRadius.circular(22),
        border: Border.all(
          color: selected ? inkTeal.withValues(alpha: 0.65) : inkBorder,
        ),
      ),
      child: Material(
        type: MaterialType.transparency,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(22),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Icon(
                  _peerIcon(peer),
                  size: 16,
                  color: selected ? inkTeal : inkTextSecondary,
                ),
                const SizedBox(width: 8),
                Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 150),
                      child: Text(
                        peer.name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: selected ? inkTextPrimary : inkTextSecondary,
                          fontSize: 13,
                          fontWeight:
                              selected ? FontWeight.w600 : FontWeight.normal,
                        ),
                      ),
                    ),
                    const SizedBox(height: 3),
                    Row(
                      mainAxisSize: MainAxisSize.min,
                      children: <Widget>[
                        for (final String route in peer.routes) ...<Widget>[
                          RouteBadge(route),
                          const SizedBox(width: 4),
                        ],
                        Text(
                          peer.shortId,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: inkTextDim,
                            fontSize: 10,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
                if (selected) ...<Widget>[
                  const SizedBox(width: 8),
                  Container(
                    width: 7,
                    height: 7,
                    decoration: const BoxDecoration(
                      color: inkTeal,
                      shape: BoxShape.circle,
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// 连接方式小标签，对应桌面端的 `.route-badge`（青字 + 半透明青描边），
/// 尺寸按移动端缩小一档。
class RouteBadge extends StatelessWidget {
  const RouteBadge(this.label, {super.key});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        border: Border.all(color: inkRouteBadgeBorder),
        borderRadius: BorderRadius.circular(5),
      ),
      child: Text(
        label,
        maxLines: 1,
        style: const TextStyle(
          color: inkRouteBadgeText,
          fontSize: 9,
          height: 1.25,
        ),
      ),
    );
  }
}

/// 根据设备名猜测是手机还是电脑（仅影响图标），照抄旧版 looksLikePhone()。
IconData _peerIcon(PeerView peer) {
  if (peer.viaSsh) return Icons.cloud_outlined;
  const List<String> phoneHints = <String>[
    'pixel',
    'xiaomi',
    'redmi',
    'huawei',
    'honor',
    'oppo',
    'vivo',
    'oneplus',
    'samsung',
    'sm-',
    'iphone',
    'mi ',
    'meizu',
    'realme',
    'iqoo',
  ];
  final String name = peer.name.toLowerCase();
  for (final String hint in phoneHints) {
    if (name.contains(hint)) return Icons.smartphone;
  }
  return Icons.computer;
}
