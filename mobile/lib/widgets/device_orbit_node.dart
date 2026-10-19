import 'package:flutter/material.dart';
import '../models.dart';
import '../theme.dart';
import 'iconsax.dart';

/// 悬浮在雷达同心圆轨道上的设备节点卡片 (Orbiting Node)
/// 采用 Iconsax `.con-svg` 超椭圆容器与双色调设备图标
class DeviceOrbitNode extends StatelessWidget {
  const DeviceOrbitNode({
    super.key,
    required this.peer,
    required this.isSelected,
    required this.onTap,
    this.top,
    this.bottom,
    this.left,
    this.right,
  });

  final PeerView peer;
  final bool isSelected;
  final VoidCallback onTap;

  final double? top;
  final double? bottom;
  final double? left;
  final double? right;

  @override
  Widget build(BuildContext context) {
    return Positioned(
      top: top,
      bottom: bottom,
      left: left,
      right: right,
      child: GestureDetector(
        onTap: onTap,
        behavior: HitTestBehavior.opaque,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOutCubic,
          padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
          decoration: BoxDecoration(
            color: isSelected
                ? surfaceActive.withValues(alpha: 0.96)
                : surfaceContainer.withValues(alpha: 0.92),
            borderRadius: BorderRadius.circular(16),
            border: Border.all(
              color: isSelected ? jade400 : surfaceBorder,
              width: isSelected ? 1.4 : 1.0,
            ),
            boxShadow: <BoxShadow>[
              BoxShadow(
                color: isSelected
                    ? jade400.withValues(alpha: 0.24)
                    : Colors.black.withValues(alpha: 0.45),
                blurRadius: isSelected ? 16 : 10,
                offset: const Offset(0, 4),
              ),
            ],
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              // Iconsax 超椭圆设备图标与右上方在线绿点
              Stack(
                clipBehavior: Clip.none,
                children: <Widget>[
                  IconsaxBox(
                    icon: _resolveDeviceIcon(peer),
                    size: 30,
                    iconSize: 16,
                    borderRadius: 9,
                    color: isSelected ? jade400 : textMuted,
                    backgroundColor: isSelected
                        ? jade400.withValues(alpha: 0.14)
                        : surfaceHigh,
                    borderColor: isSelected
                        ? jade400.withValues(alpha: 0.45)
                        : surfaceBorder,
                  ),
                  Positioned(
                    top: -2,
                    right: -2,
                    child: Container(
                      width: 8,
                      height: 8,
                      decoration: BoxDecoration(
                        color: const Color(0xFF34D399),
                        shape: BoxShape.circle,
                        border: Border.all(
                          color: surfaceContainer,
                          width: 1.5,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(width: 9),

              // 设备名称与网络/延迟
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Text(
                    peer.name,
                    style: TextStyle(
                      color: isSelected
                          ? textPrimary
                          : textPrimary.withValues(alpha: 0.92),
                      fontSize: 11.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 1),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: <Widget>[
                      Text(
                        peer.viaSsh
                            ? '中继'
                            : (peer.host.isNotEmpty ? peer.host : '已发现'),
                        style: TextStyle(
                          color: isSelected ? jade400 : textMuted,
                          fontSize: 9,
                          fontFamily: 'monospace',
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Iconsax _resolveDeviceIcon(PeerView peer) {
    if (peer.viaSsh) return Iconsax.cloud;
    final String name = peer.name.toLowerCase();
    if (name.contains('mac') || name.contains('pc') || name.contains('laptop')) {
      return Iconsax.deviceLaptop;
    }
    if (name.contains('pad') || name.contains('tablet')) {
      return Iconsax.deviceTablet;
    }
    return Iconsax.devicePhone;
  }
}
