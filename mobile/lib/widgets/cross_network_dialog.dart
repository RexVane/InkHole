import 'dart:async';

import 'package:flutter/material.dart';
import 'package:qr_flutter/qr_flutter.dart';

import '../models.dart';
import '../theme.dart';

/// 二维码配色沿用旧版 QrCode.kt：深墨模块 + 淡青底，保证各家扫码器都认。
const Color _qrModule = Color(0xFF07100E);
const Color _qrBackground = Color(0xFFE8FFF8);

/// 旧版用 `take(160)` 静默截断，不显示字数；Flutter 用 maxLength 限长时
/// 必须把默认的计数器关掉才不会多出一行 "0/160"。
Widget? _noCounter(
  BuildContext context, {
  required int currentLength,
  required bool isFocused,
  required int? maxLength,
}) =>
    null;

/// 跨网络传输面板，对应旧版 CrossNetworkUI.kt#CrossNetworkActionsDialog。
///
/// 上半区是一次性短码（生成 / 扫码 / 输入短码接收），下半区是 SSH 中继配对。
class CrossNetworkActionsDialog extends StatefulWidget {
  const CrossNetworkActionsDialog({
    super.key,
    required this.joining,
    required this.joinError,
    required this.sshReady,
    required this.initialReceiveCode,
    required this.onOneTimeSend,
    required this.onScan,
    required this.onJoinOneTime,
    required this.onCreateSshPair,
    required this.onJoinSshPair,
  });

  final ValueNotifier<bool> joining;
  final ValueNotifier<String?> joinError;
  final bool sshReady;
  final String initialReceiveCode;
  final VoidCallback onOneTimeSend;

  /// 拉起扫码，返回识别出的短码；取消或识别失败返回 null。
  final Future<String?> Function() onScan;

  /// 返回 true 表示已连上，面板自行关闭；返回 false 时保留面板让用户重试。
  final Future<bool> Function(String code) onJoinOneTime;

  final VoidCallback onCreateSshPair;
  final void Function(String code) onJoinSshPair;

  @override
  State<CrossNetworkActionsDialog> createState() =>
      _CrossNetworkActionsDialogState();
}

class _CrossNetworkActionsDialogState extends State<CrossNetworkActionsDialog> {
  late final TextEditingController _receiveCode;
  final TextEditingController _pairCode = TextEditingController();

  @override
  void initState() {
    super.initState();
    _receiveCode = TextEditingController(text: widget.initialReceiveCode);
  }

  @override
  void dispose() {
    _receiveCode.dispose();
    _pairCode.dispose();
    super.dispose();
  }

  Future<void> _join() async {
    final String code = _receiveCode.text.trim();
    if (code.isEmpty) return;
    final bool joined = await widget.onJoinOneTime(code);
    if (!joined) return;
    if (!mounted) return;
    Navigator.of(context).pop();
  }

  /// 扫到的短码直接填进输入框，由用户确认后再点「连接并接收」。
  Future<void> _scan() async {
    final String? code = await widget.onScan();
    if (code == null || code.isEmpty || !mounted) return;
    _receiveCode.value = TextEditingValue(
      text: code,
      selection: TextSelection.collapsed(offset: code.length),
    );
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: inkBgCard,
      // 对齐旧版:弹窗接近全屏,左右仅留窄边。
      insetPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 36),
      title: const Text('跨网络传输', style: TextStyle(color: inkTextPrimary)),
      content: SizedBox(
        width: double.maxFinite,
        child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            FilledButton.icon(
              onPressed: () {
                Navigator.of(context).pop();
                widget.onOneTimeSend();
              },
              icon: const Icon(Icons.send_outlined, size: 18),
              label: const Text('选择内容并生成短码'),
            ),
            const SizedBox(height: 8),
            ValueListenableBuilder<bool>(
              valueListenable: widget.joining,
              builder: (BuildContext context, bool joining, Widget? child) {
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: <Widget>[
                    Row(
                      children: <Widget>[
                        Expanded(
                          child: TextField(
                            controller: _receiveCode,
                            enabled: !joining,
                            maxLength: 160,
                            buildCounter: _noCounter,
                            decoration: const InputDecoration(
                              labelText: '一次性接收短码',
                            ),
                          ),
                        ),
                        const SizedBox(width: 4),
                        IconButton(
                          tooltip: '扫描一次性短码二维码',
                          onPressed: joining ? null : () => unawaited(_scan()),
                          icon: const Icon(Icons.qr_code_scanner),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    ValueListenableBuilder<TextEditingValue>(
                      valueListenable: _receiveCode,
                      builder: (
                        BuildContext context,
                        TextEditingValue value,
                        Widget? child,
                      ) {
                        final bool ready =
                            value.text.trim().isNotEmpty && !joining;
                        return OutlinedButton.icon(
                          onPressed: ready ? _join : null,
                          icon: const Icon(Icons.download_outlined, size: 18),
                          label: Text(joining ? '正在连接…' : '连接并接收'),
                        );
                      },
                    ),
                  ],
                );
              },
            ),
            ValueListenableBuilder<String?>(
              valueListenable: widget.joinError,
              builder: (BuildContext context, String? message, Widget? child) {
                if (message == null || message.isEmpty) {
                  return const SizedBox.shrink();
                }
                return Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    message,
                    style: const TextStyle(color: inkDanger, fontSize: 11),
                  ),
                );
              },
            ),
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 5),
              child: Divider(height: 1),
            ),
            const Text(
              'SSH 中继配对',
              style: TextStyle(
                color: inkTextPrimary,
                fontSize: 14,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 8),
            OutlinedButton.icon(
              onPressed: widget.sshReady ? widget.onCreateSshPair : null,
              icon: const Icon(Icons.key_outlined, size: 18),
              label: const Text('生成配对码'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _pairCode,
              enabled: widget.sshReady,
              maxLength: 180,
              buildCounter: _noCounter,
              decoration: const InputDecoration(labelText: 'SSH 配对码'),
            ),
            const SizedBox(height: 8),
            ValueListenableBuilder<TextEditingValue>(
              valueListenable: _pairCode,
              builder: (
                BuildContext context,
                TextEditingValue value,
                Widget? child,
              ) {
                final bool ready =
                    widget.sshReady && value.text.trim().isNotEmpty;
                return OutlinedButton(
                  onPressed:
                      ready ? () => widget.onJoinSshPair(value.text.trim()) : null,
                  child: const Text('加入配对'),
                );
              },
            ),
          ],
        ),
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('关闭'),
        ),
      ],
    );
  }
}

/// 短码展示，对应旧版 CrossNetworkUI.kt#ShortCodeDisplayDialog。
/// 未配对时下方是进度条，配对成功后变成「已安全配对」。
class ShortCodeDisplayDialog extends StatelessWidget {
  const ShortCodeDisplayDialog({
    super.key,
    required this.title,
    required this.code,
    required this.uri,
    required this.connected,
    required this.onCopy,
  });

  final String title;
  final String code;
  final String uri;
  final ValueNotifier<bool> connected;
  final VoidCallback onCopy;

  @override
  Widget build(BuildContext context) {
    final bool compact = MediaQuery.sizeOf(context).height < 500;
    final String payload = uri.isNotEmpty ? uri : code;
    return AlertDialog(
      backgroundColor: inkBgCard,
      title: Text(title, style: const TextStyle(color: inkTextPrimary)),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            if (payload.isNotEmpty)
              QrImageView(
                data: payload,
                size: compact ? 96.0 : 196.0,
                backgroundColor: _qrBackground,
                eyeStyle: const QrEyeStyle(
                  eyeShape: QrEyeShape.square,
                  color: _qrModule,
                ),
                dataModuleStyle: const QrDataModuleStyle(
                  dataModuleShape: QrDataModuleShape.square,
                  color: _qrModule,
                ),
              ),
            SizedBox(height: compact ? 4.0 : 12.0),
            SelectableText(
              code,
              textAlign: TextAlign.center,
              style: TextStyle(
                color: inkTextPrimary,
                fontFamily: 'monospace',
                fontSize: compact ? 15.0 : 17.0,
                fontWeight: FontWeight.w600,
              ),
            ),
            SizedBox(height: compact ? 4.0 : 10.0),
            ValueListenableBuilder<bool>(
              valueListenable: connected,
              builder: (BuildContext context, bool paired, Widget? child) {
                if (paired) {
                  return const Text(
                    '已安全配对',
                    style: TextStyle(color: inkTeal, fontSize: 12),
                  );
                }
                return const LinearProgressIndicator(
                  color: inkTeal,
                  backgroundColor: inkTealDim,
                );
              },
            ),
          ],
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('关闭'),
        ),
        TextButton.icon(
          onPressed: onCopy,
          icon: const Icon(Icons.content_copy, size: 16),
          label: const Text('复制短码'),
        ),
      ],
    );
  }
}

/// 接收确认，对应旧版 CrossNetworkUI.kt#WormholeOfferDialog。关闭时返回是否接收。
class WormholeOfferDialog extends StatelessWidget {
  const WormholeOfferDialog({super.key, required this.summary});

  final Map<String, dynamic> summary;

  @override
  Widget build(BuildContext context) {
    final List<String> names =
        (summary['names'] as List<dynamic>? ?? const <dynamic>[])
            .map((dynamic value) => value.toString())
            .toList(growable: false);
    return AlertDialog(
      backgroundColor: inkBgCard,
      title: const Text('接收一次性传输', style: TextStyle(color: inkTextPrimary)),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(
              summary['device_name']?.toString() ?? '未知设备',
              style: const TextStyle(
                color: inkTextPrimary,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              '${asInt(summary['item_count'])} 项 · '
              '${formatBytes(asInt(summary['total_bytes']))}',
              style: const TextStyle(color: inkTextSecondary, fontSize: 12),
            ),
            if (names.isNotEmpty) const SizedBox(height: 10),
            for (final String name in names)
              Text(
                name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: inkTextSecondary, fontSize: 12),
              ),
          ],
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('拒绝'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('接收'),
        ),
      ],
    );
  }
}

/// VPS 主机指纹确认，对应旧版 CrossNetworkUI.kt#FingerprintConfirmDialog。
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
      backgroundColor: inkBgCard,
      title: const Text('确认 VPS 主机指纹', style: TextStyle(color: inkTextPrimary)),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            serverVersion,
            style: const TextStyle(color: inkTextSecondary, fontSize: 12),
          ),
          const SizedBox(height: 10),
          SelectableText(
            fingerprint,
            style: const TextStyle(
              color: inkTextPrimary,
              fontFamily: 'monospace',
              fontSize: 12,
            ),
          ),
        ],
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(fingerprint),
          child: const Text('确认并固定'),
        ),
      ],
    );
  }
}
