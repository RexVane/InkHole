import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../theme.dart';

/// Tab 3: 配对与扫码互联视图 (Pair Qr)
class PairView extends StatefulWidget {
  const PairView({
    super.key,
    required this.listenPort,
    required this.passcode,
    required this.identityFingerprint,
    required this.onBackToRadar,
    required this.onOpenSettings,
    required this.onManualIpConnect,
    required this.onJoinWormholeCode,
  });

  final int listenPort;
  final String passcode;
  final String identityFingerprint;
  final VoidCallback onBackToRadar;
  final VoidCallback onOpenSettings;
  final VoidCallback onManualIpConnect;
  final ValueChanged<String> onJoinWormholeCode;

  @override
  State<PairView> createState() => _PairViewState();
}

class _PairViewState extends State<PairView> with SingleTickerProviderStateMixin {
  int _activeTab = 0; // 0 = 我的动态码, 1 = 扫一扫互联
  bool _copied = false;
  bool _flashlightOn = false;

  Timer? _countdownTimer;
  int _remainingSeconds = 272; // 04:32

  late final AnimationController _laserAnim;

  @override
  void initState() {
    super.initState();
    _startCountdown();
    _laserAnim = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1800),
    )..repeat(reverse: true);
  }

  void _startCountdown() {
    _countdownTimer?.cancel();
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
    _laserAnim.dispose();
    super.dispose();
  }

  String get _formattedCountdown {
    final int minutes = _remainingSeconds ~/ 60;
    final int seconds = _remainingSeconds % 60;
    return '${minutes.toString().padLeft(2, '0')}:${seconds.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: textMuted),
          onPressed: widget.onBackToRadar,
        ),
        title: const Text(
          '配对与直连',
          style: TextStyle(
            color: textPrimary,
            fontSize: 16,
            fontWeight: FontWeight.w700,
          ),
        ),
        actions: <Widget>[
          IconButton(
            onPressed: () {},
            icon: const Icon(Icons.language, size: 20),
            color: textMuted,
            splashRadius: 18,
          ),
          IconButton(
            onPressed: widget.onOpenSettings,
            icon: const Icon(Icons.settings_outlined, size: 20),
            color: textMuted,
            splashRadius: 18,
          ),
        ],
      ),
      body: SingleChildScrollView(
        physics: const BouncingScrollPhysics(),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        child: Column(
          children: <Widget>[
            // 模式切换分段控件
            _buildSegmentedSwitcher(),
            const SizedBox(height: 14),

            // 模式 1：我的动态码
            if (_activeTab == 0) _buildMyQrView() else _buildScannerView(),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }

  Widget _buildSegmentedSwitcher() {
    return Center(
      child: Container(
        constraints: const BoxConstraints(maxWidth: 340),
        padding: const EdgeInsets.all(3),
        decoration: BoxDecoration(
          color: surfaceHigh,
          borderRadius: BorderRadius.circular(24),
          border: Border.all(color: surfaceBorder),
        ),
        child: Row(
          children: <Widget>[
            Expanded(
              child: GestureDetector(
                onTap: () => setState(() => _activeTab = 0),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 180),
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  decoration: BoxDecoration(
                    color: _activeTab == 0 ? surfaceActive : Colors.transparent,
                    borderRadius: BorderRadius.circular(20),
                    border: _activeTab == 0
                        ? Border.all(color: borderLuminescent)
                        : null,
                  ),
                  alignment: Alignment.center,
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: <Widget>[
                      Icon(
                        Icons.qr_code_2,
                        size: 16,
                        color: _activeTab == 0 ? jade400 : textMuted,
                      ),
                      const SizedBox(width: 6),
                      Text(
                        '我的动态码',
                        style: TextStyle(
                          color: _activeTab == 0 ? jade400 : textMuted,
                          fontSize: 12,
                          fontWeight: _activeTab == 0
                              ? FontWeight.w700
                              : FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            Expanded(
              child: GestureDetector(
                onTap: () => setState(() => _activeTab = 1),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 180),
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  decoration: BoxDecoration(
                    color: _activeTab == 1 ? surfaceActive : Colors.transparent,
                    borderRadius: BorderRadius.circular(20),
                    border: _activeTab == 1
                        ? Border.all(color: borderLuminescent)
                        : null,
                  ),
                  alignment: Alignment.center,
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: <Widget>[
                      Icon(
                        Icons.document_scanner_outlined,
                        size: 16,
                        color: _activeTab == 1 ? jade400 : textMuted,
                      ),
                      const SizedBox(width: 6),
                      Text(
                        '扫一扫互联',
                        style: TextStyle(
                          color: _activeTab == 1 ? jade400 : textMuted,
                          fontSize: 12,
                          fontWeight: _activeTab == 1
                              ? FontWeight.w700
                              : FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildMyQrView() {
    final String codeText = widget.passcode.isNotEmpty
        ? widget.passcode
        : '7-starburst-hydra-quantum';

    return Container(
      constraints: const BoxConstraints(maxWidth: 340),
      child: Column(
        children: <Widget>[
          // QR 卡片
          Container(
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              color: surfaceContainer,
              borderRadius: BorderRadius.circular(24),
              border: Border.all(color: borderLuminescent),
              boxShadow: const <BoxShadow>[
                BoxShadow(
                  color: Color(0x66000000),
                  blurRadius: 24,
                  offset: Offset(0, 8),
                ),
              ],
            ],
            child: Column(
              children: <Widget>[
                // 顶部状态与倒计时
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    Row(
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
                        Text(
                          'QUIC:${widget.listenPort}',
                          style: const TextStyle(
                            color: jade400,
                            fontSize: 11,
                            fontFamily: 'monospace',
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                      decoration: BoxDecoration(
                        color: surfaceHigh,
                        borderRadius: BorderRadius.circular(16),
                      ),
                      child: Row(
                        children: <Widget>[
                          const Icon(Icons.autorenew, color: jade400, size: 12),
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
                          const SizedBox(width: 3),
                          const Text(
                            '有效',
                            style: TextStyle(color: textDim, fontSize: 10),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 16),

                // 二维码核心 (带四角赛博准星)
                Container(
                  width: 220,
                  height: 220,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: surfaceLowest,
                    borderRadius: BorderRadius.circular(18),
                    border: Border.all(color: surfaceBorder),
                  ),
                  child: Stack(
                    alignment: Alignment.center,
                    children: <Widget>[
                      // 四角准星
                      const Positioned(
                        top: 2,
                        left: 2,
                        child: _CornerReticle(isTop: true, isLeft: true),
                      ),
                      const Positioned(
                        top: 2,
                        right: 2,
                        child: _CornerReticle(isTop: true, isLeft: false),
                      ),
                      const Positioned(
                        bottom: 2,
                        left: 2,
                        child: _CornerReticle(isTop: false, isLeft: true),
                      ),
                      const Positioned(
                        bottom: 2,
                        right: 2,
                        child: _CornerReticle(isTop: false, isLeft: false),
                      ),

                      // 二维码本体
                      QrImageView(
                        data: 'inkhole:$codeText',
                        version: QrVersions.auto,
                        size: 180,
                        eyeStyle: const QrEyeStyle(
                          eyeShape: QrEyeShape.square,
                          color: jade400,
                        ),
                        dataModuleStyle: const QrDataModuleStyle(
                          dataModuleShape: QrDataModuleShape.square,
                          color: textPrimary,
                        ),
                      ),

                      // 中心微型墨洞图腾点
                      Container(
                        width: 26,
                        height: 26,
                        decoration: BoxDecoration(
                          color: bgAbyss,
                          shape: BoxShape.circle,
                          border: Border.all(color: jade400, width: 1.5),
                        ),
                        child: Center(
                          child: Container(
                            width: 8,
                            height: 8,
                            decoration: const BoxDecoration(
                              color: jade400,
                              shape: BoxShape.circle,
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 16),

                // 口令密文框
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                  decoration: BoxDecoration(
                    color: surfaceHigh,
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: surfaceBorder),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: <Widget>[
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: <Widget>[
                          const Row(
                            children: <Widget>[
                              Icon(Icons.vpn_key, color: jade400, size: 13),
                              SizedBox(width: 4),
                              Text(
                                '口令密文 / Secret Token',
                                style: TextStyle(color: textMuted, fontSize: 10),
                              ),
                            ],
                          ),
                          GestureDetector(
                            onTap: () {
                              Clipboard.setData(ClipboardData(text: codeText));
                              setState(() => _copied = true);
                              Future<void>.delayed(const Duration(seconds: 2), () {
                                if (mounted) setState(() => _copied = false);
                              });
                            },
                            child: Row(
                              children: <Widget>[
                                Icon(
                                  _copied ? Icons.check : Icons.copy,
                                  color: jade400,
                                  size: 12,
                                ),
                                const SizedBox(width: 2),
                                Text(
                                  _copied ? '已复制' : '复制',
                                  style: const TextStyle(
                                    color: jade400,
                                    fontSize: 10,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 6),
                      Text(
                        '# $codeText',
                        style: const TextStyle(
                          color: jade400,
                          fontSize: 13,
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

          // 指引卡片
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: surfaceLow,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: surfaceBorder),
            ),
            child: const Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Icon(Icons.wifi_tethering, color: jade400, size: 20),
                SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        '点对点极速桥接',
                        style: TextStyle(
                          color: textPrimary,
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      SizedBox(height: 2),
                      Text(
                        '让对端手机打开墨洞点击 [扫码配对]，或在通道栏直接输入上方暗号即可秒级建立直连通道。',
                        style: TextStyle(
                          color: textMuted,
                          fontSize: 11,
                          height: 1.4,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 14),

          // 开启摄像头扫码 CTA 按钮
          SizedBox(
            width: double.infinity,
            height: 44,
            child: ElevatedButton.icon(
              onPressed: () => setState(() => _activeTab = 1),
              icon: const Icon(Icons.photo_camera, size: 18, color: bgAbyss),
              label: const Text(
                '开启摄像头扫码',
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
                  borderRadius: BorderRadius.circular(22),
                ),
              ),
            ),
          ),
          const SizedBox(height: 8),

          // 直接输入 IP 入口
          GestureDetector(
            onTap: widget.onManualIpConnect,
            child: const Padding(
              padding: EdgeInsets.symmetric(vertical: 4),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: <Widget>[
                  Icon(Icons.link, color: textMuted, size: 14),
                  SizedBox(width: 4),
                  Text(
                    '直接输入对端 IP / 域名连接',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 14),

          // 对等安全探针卡片
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: surfaceContainer,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: borderLuminescent),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                const Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    Row(
                      children: <Widget>[
                        Icon(Icons.security, color: jade400, size: 16),
                        SizedBox(width: 6),
                        Text(
                          '对等加密与打洞探针',
                          style: TextStyle(
                            color: textPrimary,
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                    Text(
                      'ED25519-P2P',
                      style: TextStyle(
                        color: badgeRoute,
                        fontSize: 9,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                  decoration: BoxDecoration(
                    color: surfaceLowest,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: <Widget>[
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: <Widget>[
                            const Text(
                              '通道公钥指纹 (BLAKE3)',
                              style: TextStyle(color: textDim, fontSize: 9),
                            ),
                            Text(
                              'ed25519:${widget.identityFingerprint}:quic-v1',
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: textMuted,
                                fontSize: 10,
                                fontFamily: 'monospace',
                              ),
                            ),
                          ],
                        ),
                      ),
                      const Icon(Icons.verified, color: jade400, size: 16),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildScannerView() {
    return Container(
      constraints: const BoxConstraints(maxWidth: 340),
      child: Column(
        children: <Widget>[
          // 仿相机取景器
          Container(
            width: double.infinity,
            height: 320,
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(24),
              border: Border.all(color: borderLuminescent),
            ),
            child: Stack(
              alignment: Alignment.center,
              children: <Widget>[
                // 扫描框与四角
                Container(
                  width: 220,
                  height: 220,
                  decoration: BoxDecoration(
                    border: Border.all(color: surfaceBorder),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Stack(
                    children: <Widget>[
                      const Positioned(
                        top: -1,
                        left: -1,
                        child: _CornerReticle(isTop: true, isLeft: true),
                      ),
                      const Positioned(
                        top: -1,
                        right: -1,
                        child: _CornerReticle(isTop: true, isLeft: false),
                      ),
                      const Positioned(
                        bottom: -1,
                        left: -1,
                        child: _CornerReticle(isTop: false, isLeft: true),
                      ),
                      const Positioned(
                        bottom: -1,
                        right: -1,
                        child: _CornerReticle(isTop: false, isLeft: false),
                      ),

                      // 动画量子激光扫描线
                      AnimatedBuilder(
                        animation: _laserAnim,
                        builder: (BuildContext context, Widget? child) {
                          return Positioned(
                            top: _laserAnim.value * 200,
                            left: 0,
                            right: 0,
                            child: Container(
                              height: 2,
                              decoration: BoxDecoration(
                                gradient: const LinearGradient(
                                  colors: <Color>[
                                    Colors.transparent,
                                    jade400,
                                    Colors.transparent,
                                  ],
                                ),
                                boxShadow: <BoxShadow>[
                                  BoxShadow(
                                    color: jade400.withValues(alpha: 0.8),
                                    blurRadius: 8,
                                    spreadRadius: 1,
                                  ),
                                ],
                              ),
                            ),
                          );
                        },
                      ),
                    ],
                  ),
                ),

                // 底部文字提示
                Positioned(
                  bottom: 16,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
                    decoration: BoxDecoration(
                      color: surfaceContainer.withValues(alpha: 0.9),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: surfaceBorder),
                    ),
                    child: const Row(
                      mainAxisSize: MainAxisSize.min,
                      children: <Widget>[
                        Icon(Icons.center_focus_strong, color: jade400, size: 14),
                        SizedBox(width: 6),
                        Text(
                          '将对端墨洞二维码置于框内',
                          style: TextStyle(color: textPrimary, fontSize: 11),
                        ),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 14),

          // 补光灯与相册导入
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: <Widget>[
              ElevatedButton.icon(
                onPressed: () {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text('已选择相册导入二维码'),
                      duration: Duration(seconds: 1),
                    ),
                  );
                },
                icon: const Icon(Icons.photo_library, size: 16, color: jade400),
                label: const Text(
                  '相册导入',
                  style: TextStyle(color: textPrimary, fontSize: 11),
                ),
                style: ElevatedButton.styleFrom(
                  backgroundColor: surfaceContainer,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(20),
                    side: const BorderSide(color: surfaceBorder),
                  ),
                ),
              ),
              ElevatedButton.icon(
                onPressed: () => setState(() => _flashlightOn = !_flashlightOn),
                icon: Icon(
                  _flashlightOn ? Icons.flash_on : Icons.flash_off,
                  size: 16,
                  color: jade400,
                ),
                label: Text(
                  _flashlightOn ? '关闭补光' : '补光灯',
                  style: const TextStyle(color: textPrimary, fontSize: 11),
                ),
                style: ElevatedButton.styleFrom(
                  backgroundColor: surfaceContainer,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(20),
                    side: const BorderSide(color: surfaceBorder),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),

          // 切回我的动态码按钮
          SizedBox(
            width: double.infinity,
            height: 44,
            child: ElevatedButton.icon(
              onPressed: () => setState(() => _activeTab = 0),
              icon: const Icon(Icons.qr_code, size: 18, color: bgAbyss),
              label: const Text(
                '展示我的动态码',
                style: TextStyle(
                  color: bgAbyss,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
              style: ElevatedButton.styleFrom(
                backgroundColor: jade400,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(22),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _CornerReticle extends StatelessWidget {
  const _CornerReticle({required this.isTop, required this.isLeft});

  final bool isTop;
  final bool isLeft;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 14,
      height: 14,
      decoration: BoxDecoration(
        border: Border(
          top: isTop
              ? const BorderSide(color: jade400, width: 2.5)
              : BorderSide.none,
          bottom: !isTop
              ? const BorderSide(color: jade400, width: 2.5)
              : BorderSide.none,
          left: isLeft
              ? const BorderSide(color: jade400, width: 2.5)
              : BorderSide.none,
          right: !isLeft
              ? const BorderSide(color: jade400, width: 2.5)
              : BorderSide.none,
        ),
      ),
    );
  }
}
