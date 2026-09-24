import 'package:flutter/material.dart';
import '../models.dart';
import '../theme.dart';

/// 悬浮在雷达同心圆轨道上的设备节点卡片 (Orbiting Node)
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
          duration: const Duration(milliseconds: 200),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
          decoration: BoxDecoration(
            color: isSelected
                ? surfaceActive.withValues(alpha: 0.95)
                : surfaceContainer.withValues(alpha: 0.90),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(
              color: isSelected ? jade400 : surfaceBorder,
              width: isSelected ? 1.5 : 1.0,
            ),
            boxShadow: <BoxShadow>[
              BoxShadow(
                color: isSelected
                    ? jade400.withValues(alpha: 0.22)
                    : Colors.black.withValues(alpha: 0.4),
                blurRadius: isSelected ? 14 : 8,
                offset: const Offset(0, 3),
              ),
            ],
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              // 设备图标与右上方在线绿点
              Stack(
                clipBehavior: Clip.none,
                children: <Widget>[
                  Container(
                    width: 28,
                    height: 28,
                    decoration: BoxDecoration(
                      color: surfaceHigh,
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                        color: isSelected
                            ? jade400.withValues(alpha: 0.5)
                            : surfaceBorder,
                      ),
                    ),
                    child: Icon(
                      _resolveDeviceIcon(peer),
                      color: isSelected ? jade400 : textMuted,
                      size: 15,
                    ),
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
              const SizedBox(width: 8),

              // 设备名称与网络/延迟
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Text(
                    peer.name,
                    style: TextStyle(
                      color: isSelected ? textPrimary : textPrimary.withValues(alpha: 0.9),
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 1),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: <Widget>[
                      Text(
                        peer.viaSsh
                            ? 'Tailscale · 12ms'
                            : (peer.host.isNotEmpty ? peer.host : 'QUIC 直连'),
                        style: TextStyle(
                          color: isSelected ? jade400 : textMuted,
                          fontSize: 9,
                          fontFamily: 'monospace',
                        ),
                      ),
                      if (!peer.viaSsh) ...<Widget>[
                        const Text(
                          ' · ',
                          style: TextStyle(color: textDim, fontSize: 9),
                        ),
                        const Text(
                          '580M',
                          style: TextStyle(
                            color: Color(0xFF34D399),
                            fontSize: 9,
                            fontFamily: 'monospace',
                          ),
                        ),
                      ],
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

  IconData _resolveDeviceIcon(PeerView peer) {
    if (peer.viaSsh) return Icons.cloud_outlined;
    final String name = peer.name.toLowerCase();
    if (name.contains('mac') || name.contains('pc') || name.contains('laptop')) {
      return Icons.laptop_mac;
    }
    if (name.contains('pad') || name.contains('tablet')) {
      return Icons.tablet_mac;
    }
    return Icons.phone_android;
  }
}
