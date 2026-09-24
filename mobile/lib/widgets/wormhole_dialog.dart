import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../theme.dart';

/// 暗号直连模态浮层 (Wormhole Code Pairing Modal)
///
/// 1:1 还原设计图 2：
/// - 发送暗号 / 输入暗号分段切换
/// - 发光大号一次性握手口令 Hero Box
/// - 复制口令、动态二维码展示、5分钟倒计时
/// - 输入暗号、粘贴、穿透直连
class WormholePairingDialog extends StatefulWidget {
  const WormholePairingDialog({
    super.key,
    required this.passcode,
    required this.isGenerating,
    required this.onRefreshCode,
    required this.onJoinCode,
    this.initialTab = 0,
  });

  /// 当前生成的暗号 (例如 7-starburst-hydra)
  final String passcode;

  /// 是否正在向 rendezvous 生成暗号中
  final bool isGenerating;

  /// 刷新/重新生成口令
  final VoidCallback onRefreshCode;

  /// 提交输入的暗号进行连接
  final ValueChanged<String> onJoinCode;

  final int initialTab;

  @override
  State<WormholePairingDialog> createState() => _WormholePairingDialogState();
}

class _WormholePairingDialogState extends State<WormholePairingDialog> {
  late int _tabIndex; // 0 = 发送暗号, 1 = 输入暗号
  final TextEditingController _inputController = TextEditingController();
  bool _copied = false;
  bool _showQrCode = false;

  Timer? _countdownTimer;
  int _remainingSeconds = 300; // 5分钟

  @override
  void initState() {
    super.initState();
    _tabIndex = widget.initialTab;
    _startCountdown();
  }

  void _startCountdown() {
    _countdownTimer?.cancel();
    _remainingSeconds = 300;
    _countdownTimer = Timer.periodic(const Duration(seconds: 1), (Timer timer) {
      if (mounted) {
        setState(() {
          if (_remainingSeconds > 0) {
            _remainingSeconds--;
          } else {
            _remainingSeconds = 300;
          }
        });
      }
    });
  }

  @override
  void dispose() {
    _countdownTimer?.cancel();
    _inputController.dispose();
    super.dispose();
  }

  String get _formattedCountdown {
    final int minutes = _remainingSeconds ~/ 60;
    final int seconds = _remainingSeconds % 60;
    return '${minutes.toString().padLeft(2, '0')}:${seconds.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: Colors.transparent,
      insetPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 24),
      child: Container(
        decoration: BoxDecoration(
          color: surfaceContainer.withValues(alpha: 0.96),
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: borderLuminescent, width: 1.2),
          boxShadow: <BoxShadow>[
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.65),
              blurRadius: 36,
              offset: const Offset(0, 10),
            ),
          ],
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            // 顶部高光微细横线
            Container(
              height: 2,
              decoration: const BoxDecoration(
                borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
                gradient: LinearGradient(
                  colors: <Color>[
                    Colors.transparent,
                    jade400,
                    Colors.transparent,
                  ],
                ),
              ),
            ),

            // 标题与关闭按钮
            Padding(
              padding: const EdgeInsets.fromLTRB(18, 14, 14, 10),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: <Widget>[
                  Row(
                    children: <Widget>[
                      Container(
                        width: 32,
                        height: 32,
                        decoration: BoxDecoration(
                          color: surfaceActive,
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(
                            color: jade400.withValues(alpha: 0.25),
                          ),
                        ),
                        child: const Icon(Icons.key, color: jade400, size: 18),
                      ),
                      const SizedBox(width: 10),
                      const Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: <Widget>[
                          Text(
                            '暗号直连',
                            style: TextStyle(
                              color: textPrimary,
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                              letterSpacing: 0.5,
                            ),
                          ),
                          Text(
                            'Wormhole Code Pairing',
                            style: TextStyle(
                              color: textMuted,
                              fontSize: 9,
                              fontFamily: 'monospace',
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                  IconButton(
                    onPressed: () => Navigator.of(context).pop(),
                    icon: const Icon(Icons.close, color: textMuted, size: 20),
                    splashRadius: 18,
                  ),
                ],
              ),
            ),

            // 双 Tab 分段切换器
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
              child: Container(
                padding: const EdgeInsets.all(3),
                decoration: BoxDecoration(
                  color: surfaceLowest,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: surfaceBorder),
                ),
                child: Row(
                  children: <Widget>[
                    Expanded(
                      child: GestureDetector(
                        onTap: () => setState(() => _tabIndex = 0),
                        child: AnimatedContainer(
                          duration: const Duration(milliseconds: 180),
                          padding: const EdgeInsets.symmetric(vertical: 8),
                          decoration: BoxDecoration(
                            color: _tabIndex == 0 ? surfaceActive : Colors.transparent,
                            borderRadius: BorderRadius.circular(9),
                            border: _tabIndex == 0
                                ? Border.all(color: borderLuminescent)
                                : null,
                          ),
                          alignment: Alignment.center,
                          child: Text(
                            '发送暗号',
                            style: TextStyle(
                              color: _tabIndex == 0 ? jade400 : textMuted,
                              fontSize: 12,
                              fontWeight: _tabIndex == 0 ? FontWeight.w700 : FontWeight.w500,
                            ),
                          ),
                        ),
                      ),
                    ),
                    Expanded(
                      child: GestureDetector(
                        onTap: () => setState(() => _tabIndex = 1),
                        child: AnimatedContainer(
                          duration: const Duration(milliseconds: 180),
                          padding: const EdgeInsets.symmetric(vertical: 8),
                          decoration: BoxDecoration(
                            color: _tabIndex == 1 ? surfaceActive : Colors.transparent,
                            borderRadius: BorderRadius.circular(9),
                            border: _tabIndex == 1
                                ? Border.all(color: borderLuminescent)
                                : null,
                          ),
                          alignment: Alignment.center,
                          child: Text(
                            '输入暗号',
                            style: TextStyle(
                              color: _tabIndex == 1 ? jade400 : textMuted,
                              fontSize: 12,
                              fontWeight: _tabIndex == 1 ? FontWeight.w700 : FontWeight.w500,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),

            // 内容视图区域
            Padding(
              padding: const EdgeInsets.all(16),
              child: _tabIndex == 0 ? _buildSendPanel() : _buildReceivePanel(),
            ),
          ],
        ),
      ),
    );
  }

  // 模式 1：发送暗号面板
  Widget _buildSendPanel() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        // Hero Box (暗号或二维码)
        Container(
          padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 14),
          decoration: BoxDecoration(
            color: surfaceLowest,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: borderLuminescent),
            boxShadow: const <BoxShadow>[
              BoxShadow(
                color: Color(0x66000000),
                blurRadius: 10,
                offset: Offset(0, 2),
              ),
            ],
          ),
          child: Column(
            children: <Widget>[
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: <Widget>[
                  Container(
                    width: 6,
                    height: 6,
                    decoration: const BoxDecoration(
                      color: jade400,
                      shape: BoxShape.circle,
                    ),
                  ),
                  const SizedBox(width: 6),
                  const Text(
                    '一次性握手口令',
                    style: TextStyle(
                      color: textMuted,
                      fontSize: 10,
                      fontWeight: FontWeight.w600,
                      letterSpacing: 1.0,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 10),

              if (_showQrCode && widget.passcode.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: surfaceContainer,
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: QrImageView(
                      data: widget.passcode,
                      version: QrVersions.auto,
                      size: 150.0,
                      eyeStyle: const QrEyeStyle(
                        eyeShape: QrEyeShape.square,
                        color: jade400,
                      ),
                      dataModuleStyle: const QrDataModuleStyle(
                        dataModuleShape: QrDataModuleShape.square,
                        color: textPrimary,
                      ),
                    ),
                  ),
                )
              else
                SelectableText.rich(
                  TextSpan(
                    children: <TextSpan>[
                      const TextSpan(
                        text: '# ',
                        style: TextStyle(
                          color: textDim,
                          fontSize: 20,
                          fontWeight: FontWeight.w300,
                        ),
                      ),
                      TextSpan(
                        text: widget.passcode.isNotEmpty
                            ? widget.passcode
                            : (widget.isGenerating ? '正在生成...' : '7-starburst-hydra'),
                        style: const TextStyle(
                          color: jade400,
                          fontSize: 22,
                          fontWeight: FontWeight.w800,
                          fontFamily: 'monospace',
                          letterSpacing: 0.5,
                          shadows: <Shadow>[
                            Shadow(
                              color: jade400,
                              blurRadius: 16,
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                  textAlign: TextAlign.center,
                ),
              const SizedBox(height: 6),
              const Text(
                '对方输入即可建立点对点直连',
                style: TextStyle(color: textMuted, fontSize: 11),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),

        // 快捷操作按钮：复制暗号 / 动态二维码
        Row(
          children: <Widget>[
            Expanded(
              child: SizedBox(
                height: 40,
                child: ElevatedButton.icon(
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: widget.passcode));
                    setState(() => _copied = true);
                    Future<void>.delayed(const Duration(seconds: 2), () {
                      if (mounted) setState(() => _copied = false);
                    });
                  },
                  icon: Icon(
                    _copied ? Icons.check : Icons.copy,
                    size: 16,
                    color: jade400,
                  ),
                  label: Text(
                    _copied ? '已复制!' : '复制暗号',
                    style: const TextStyle(
                      color: textPrimary,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: surfaceHigh,
                    elevation: 0,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(10),
                      side: const BorderSide(color: surfaceBorder),
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: SizedBox(
                height: 40,
                child: ElevatedButton.icon(
                  onPressed: () => setState(() => _showQrCode = !_showQrCode),
                  icon: Icon(
                    _showQrCode ? Icons.text_fields : Icons.qr_code_2,
                    size: 17,
                    color: jade400,
                  ),
                  label: Text(
                    _showQrCode ? '文字口令' : '动态二维码',
                    style: const TextStyle(
                      color: textPrimary,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: surfaceHigh,
                    elevation: 0,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(10),
                      side: const BorderSide(color: surfaceBorder),
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),

        // 连接状态与倒计时胶囊
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: surfaceLowest,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: surfaceBorder),
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Container(
                    width: 7,
                    height: 7,
                    decoration: const BoxDecoration(
                      color: jade400,
                      shape: BoxShape.circle,
                    ),
                  ),
                  const SizedBox(width: 8),
                  const Text(
                    '等待对方连接...',
                    style: TextStyle(
                      color: textPrimary,
                      fontSize: 11,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ],
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: surfaceActive,
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(color: borderLuminescent),
                ),
                child: Row(
                  children: <Widget>[
                    const Icon(Icons.timer_outlined, color: textMuted, size: 12),
                    const SizedBox(width: 4),
                    Text(
                      _formattedCountdown,
                      style: const TextStyle(
                        color: jade400,
                        fontSize: 11,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 14),

        // 主操作按钮
        SizedBox(
          height: 44,
          child: ElevatedButton.icon(
            onPressed: widget.onRefreshCode,
            icon: const Icon(Icons.bolt, size: 18, color: bgAbyss),
            label: const Text(
              '开启通道穿透直连',
              style: TextStyle(
                color: bgAbyss,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
            style: ElevatedButton.styleFrom(
              backgroundColor: jade400,
              elevation: 4,
              shadowColor: jade400.withValues(alpha: 0.35),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
        ),
      ],
    );
  }

  // 模式 2：输入暗号面板
  Widget _buildReceivePanel() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: surfaceLowest,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: surfaceBorder),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const Text(
                '输入发送方告知的暗号',
                style: TextStyle(color: textMuted, fontSize: 12),
              ),
              const SizedBox(height: 10),
              Stack(
                alignment: Alignment.centerRight,
                children: <Widget>[
                  TextField(
                    controller: _inputController,
                    style: const TextStyle(
                      color: jade400,
                      fontSize: 14,
                      fontFamily: 'monospace',
                      fontWeight: FontWeight.w700,
                    ),
                    decoration: InputDecoration(
                      hintText: '例如: 7-starburst-hydra',
                      hintStyle: const TextStyle(color: textDim, fontSize: 13),
                      contentPadding: const EdgeInsets.only(
                        left: 14,
                        right: 64,
                        top: 12,
                        bottom: 12,
                      ),
                    ),
                  ),
                  Positioned(
                    right: 6,
                    child: TextButton(
                      onPressed: () async {
                        final ClipboardData? data =
                            await Clipboard.getData('text/plain');
                        if (data?.text != null) {
                          _inputController.text = data!.text!.trim();
                        }
                      },
                      style: TextButton.styleFrom(
                        backgroundColor: surfaceActive,
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                        minimumSize: Size.zero,
                        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(8),
                        ),
                      ),
                      child: const Text(
                        '粘贴',
                        style: TextStyle(
                          color: jade400,
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
        const SizedBox(height: 18),

        // 主操作按钮
        SizedBox(
          height: 44,
          child: ElevatedButton.icon(
            onPressed: () {
              final String code = _inputController.text.trim();
              if (code.isNotEmpty) {
                widget.onJoinCode(code);
                Navigator.of(context).pop();
              }
            },
            icon: const Icon(Icons.bolt, size: 18, color: bgAbyss),
            label: const Text(
              '连接暗号并拉取',
              style: TextStyle(
                color: bgAbyss,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
            style: ElevatedButton.styleFrom(
              backgroundColor: jade400,
              elevation: 4,
              shadowColor: jade400.withValues(alpha: 0.35),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
        ),
      ],
    );
  }
}
