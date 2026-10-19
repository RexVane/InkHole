import 'package:flutter/material.dart';

import '../theme.dart';

/// VPS 主机指纹确认弹窗。
/// 「确认并固定」返回指纹字符串，取消返回 null。
class FingerprintConfirmDialog extends StatelessWidget {
  const FingerprintConfirmDialog({
    super.key,
    required this.fingerprint,
    required this.serverVersion,
  });

  final String fingerprint;
  final String serverVersion;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: surfaceContainer,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(14),
        side: const BorderSide(color: surfaceBorder),
      ),
      title: const Text(
        '确认 VPS 主机指纹',
        style: TextStyle(
          color: textPrimary,
          fontSize: 16,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.6,
        ),
      ),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            serverVersion,
            style: const TextStyle(color: textMuted, fontSize: 12),
          ),
          const SizedBox(height: 12),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: surfaceBorder),
            ),
            child: SelectableText(
              fingerprint,
              style: const TextStyle(
                color: jade400,
                fontFamily: 'monospace',
                fontSize: 12,
                height: 1.4,
              ),
            ),
          ),
        ],
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消', style: TextStyle(color: textMuted)),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(fingerprint),
          style: FilledButton.styleFrom(
            backgroundColor: jade400,
            foregroundColor: bgAbyss,
          ),
          child: const Text(
            '确认并固定',
            style: TextStyle(fontWeight: FontWeight.w700),
          ),
        ),
      ],
    );
  }
}

