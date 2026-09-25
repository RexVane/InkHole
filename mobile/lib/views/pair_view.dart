import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../models.dart';
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
    this.passcodeExpiresAt,
    this.cameraEnabled = false,
    this.onScanned,
    this.onScanImage,
    this.onPickAndCreateWormhole,
    this.onPasscodeExpired,
    this.scanRequest = 0,
  });

  final int listenPort;
  final String passcode;
  final DateTime? passcodeExpiresAt;
  final String identityFingerprint;
  final VoidCallback onBackToRadar;
  final VoidCallback onOpenSettings;
  final VoidCallback onManualIpConnect;
  final ValueChanged<String> onJoinWormholeCode;

  /// 配对页正在前台时才打开框内相机，避免切到别的页还占着摄像头。
  final bool cameraEnabled;
  final ValueChanged<String>? onScanned;
  final VoidCallback? onScanImage;
  final VoidCallback? onPickAndCreateWormhole;
  final VoidCallback? onPasscodeExpired;

  /// 雷达上的「扫码配对」每次加一，用来切到扫一扫并打开相机。
  final int scanRequest;

  @override
  State<PairView> createState() => _PairViewState();
}

class _PairViewState extends State<PairView> with SingleTickerProviderStateMixin {
  int _activeTab = 0; // 0 = 我的动态码, 1 = 扫一扫互联
  late int _seenScanRequest;
  bool _copied = false;
  bool _expiryNotified = false;

  Timer? _countdownTimer;

  late final AnimationController _laserAnim;

  @override
  void initState() {
    super.initState();
    _seenScanRequest = widget.scanRequest;
    if (widget.scanRequest > 0) _activeTab = 1;
    _countdownTimer = Timer.periodic(const Duration(seconds: 1), (Timer timer) {
      _tickCountdown();
    });
    _laserAnim = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1800),
    )..repeat(reverse: true);
  }

  @override
  void didUpdateWidget(covariant PairView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.passcode != widget.passcode ||
        oldWidget.passcodeExpiresAt != widget.passcodeExpiresAt) {
      _expiryNotified = false;
    }
    if (widget.scanRequest != _seenScanRequest) {
      _seenScanRequest = widget.scanRequest;
      setState(() => _activeTab = 1);
    }
  }

  int get _remainingSeconds {
    final DateTime? expiry = widget.passcodeExpiresAt;
    if (widget.passcode.trim().isEmpty || expiry == null) return 0;
    final int next = expiry.difference(DateTime.now()).inSeconds;
    return next > 0 ? next : 0;
  }

  void _tickCountdown() {
    if (!mounted) return;
    final DateTime? expiry = widget.passcodeExpiresAt;
    final bool hasCode = widget.passcode.trim().isNotEmpty;
    final int next = (!hasCode || expiry == null)
        ? 0
        : expiry.difference(DateTime.now()).inSeconds;
    if (hasCode && expiry != null && next <= 0 && !_expiryNotified) {
      _expiryNotified = true;
      widget.onPasscodeExpired?.call();
    }
    if (next > 0) _expiryNotified = false;
    setState(() {});
  }

  @override
  void dispose() {
    _countdownTimer?.cancel();
    _laserAnim.dispose();
    super.dispose();
  }

  String get _formattedCountdown {
    if (widget.passcode.trim().isEmpty || widget.passcodeExpiresAt == null) {
      return '未生成';
    }
    if (_remainingSeconds <= 0) return '已过期';
    final int minutes = _remainingSeconds ~/ 60;
    final int seconds = _remainingSeconds % 60;
    return '${minutes.toString().padLeft(2, '0')}:${seconds.toString().padLeft(2, '0')}';
  }

  bool get _countdownActive =>
      widget.passcode.trim().isNotEmpty &&
      widget.passcodeExpiresAt != null &&
      _remainingSeconds > 0;

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
    final String codeText = widget.passcode.trim();
    final bool hasCode = codeText.isNotEmpty;

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
            ),
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
                          if (_countdownActive) ...<Widget>[
                            const SizedBox(width: 3),
                            const Text(
                              '有效',
                              style: TextStyle(color: textDim, fontSize: 10),
                            ),
                          ],
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

                      if (hasCode)
                      QrImageView(
                        data: wormholeReceiveUri(codeText),
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
                      )
                      else
                        const Padding(
                          padding: EdgeInsets.symmetric(horizontal: 16),
                          child: Text(
                            '选择文件后生成暗号',
                            textAlign: TextAlign.center,
                            style: TextStyle(color: textMuted, fontSize: 12),
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
                              if (!hasCode) return;
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
                        hasCode ? '# $codeText' : '# 尚未生成',
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

          if (widget.onPickAndCreateWormhole != null) ...<Widget>[
            SizedBox(
              width: double.infinity,
              height: 42,
              child: OutlinedButton.icon(
                onPressed: widget.onPickAndCreateWormhole,
                icon: const Icon(Icons.key, size: 16, color: jade400),
                label: const Text(
                  '选择待发文件生成专属暗号',
                  style: TextStyle(
                    color: textPrimary,
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: surfaceBorder),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(20),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 10),
          ],

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
                    '添加对端地址，发现后再发送',
                    style: TextStyle(color: textMuted, fontSize: 11),
                  ),
                ],
              ),
            ),
          ),
          if (widget.identityFingerprint.isNotEmpty) ...<Widget>[
            const SizedBox(height: 14),
            Text(
              '证书指纹 ${widget.identityFingerprint}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                color: textMuted,
                fontSize: 10,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildScannerView() {
    return Container(
      constraints: const BoxConstraints(maxWidth: 340),
      child: Column(
        children: <Widget>[
          _LiveFinder(
            active: widget.cameraEnabled && _activeTab == 1,
            laser: _laserAnim,
            onScanned: widget.onScanned ?? widget.onJoinWormholeCode,
            onScanImage: widget.onScanImage,
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

class _LiveFinder extends StatefulWidget {
  const _LiveFinder({
    required this.active,
    required this.laser,
    required this.onScanned,
    this.onScanImage,
  });

  final bool active;
  final Animation<double> laser;
  final ValueChanged<String> onScanned;
  final VoidCallback? onScanImage;

  @override
  State<_LiveFinder> createState() => _LiveFinderState();
}

class _LiveFinderState extends State<_LiveFinder> {
  MobileScannerController? _controller;
  String? _lastRaw;
  bool _torchOn = false;

  @override
  void initState() {
    super.initState();
    if (widget.active) {
      _controller = MobileScannerController(
        detectionSpeed: DetectionSpeed.normal,
        formats: const <BarcodeFormat>[BarcodeFormat.qrCode],
        facing: CameraFacing.back,
        torchEnabled: _torchOn,
      );
    }
  }

  @override
  void didUpdateWidget(covariant _LiveFinder oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.active != widget.active) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _syncCamera();
      });
    }
  }

  void _syncCamera() {
    if (widget.active && _controller == null) {
      setState(() {
        _controller = MobileScannerController(
          detectionSpeed: DetectionSpeed.normal,
          formats: const <BarcodeFormat>[BarcodeFormat.qrCode],
          facing: CameraFacing.back,
          torchEnabled: _torchOn,
        );
      });
      return;
    }
    if (!widget.active && _controller != null) {
      final MobileScannerController old = _controller!;
      setState(() {
        _controller = null;
        _lastRaw = null;
      });
      old.dispose();
    }
  }

  void _onDetect(BarcodeCapture capture) {
    for (final Barcode barcode in capture.barcodes) {
      final String raw = (barcode.rawValue ?? '').trim();
      if (raw.isEmpty || raw == _lastRaw) continue;
      _lastRaw = raw;
      widget.onScanned(raw);
      return;
    }
  }

  Future<void> _toggleTorch() async {
    final MobileScannerController? controller = _controller;
    if (controller == null) return;
    await controller.toggleTorch();
    if (!mounted) return;
    setState(() => _torchOn = !_torchOn);
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final MobileScannerController? controller = _controller;
    return Column(
      children: <Widget>[
        ClipRRect(
          borderRadius: BorderRadius.circular(24),
          child: Container(
            width: double.infinity,
            height: 320,
            decoration: BoxDecoration(
              color: surfaceLowest,
              border: Border.all(color: borderLuminescent),
            ),
            child: Stack(
              alignment: Alignment.center,
              children: <Widget>[
                if (controller != null)
                  MobileScanner(
                    controller: controller,
                    onDetect: _onDetect,
                    fit: BoxFit.cover,
                    errorBuilder: (BuildContext context, MobileScannerException error, Widget? child) {
                      return const ColoredBox(
                        color: surfaceLowest,
                        child: Center(
                          child: Padding(
                            padding: EdgeInsets.all(24),
                            child: Text(
                              '需要相机权限，才能在这个框里直接扫描',
                              textAlign: TextAlign.center,
                              style: TextStyle(color: textMuted, fontSize: 12),
                            ),
                          ),
                        ),
                      );
                    },
                  )
                else
                  const ColoredBox(color: surfaceLowest),
                IgnorePointer(
                  child: Container(
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
                        AnimatedBuilder(
                          animation: widget.laser,
                          builder: (BuildContext context, Widget? child) {
                            return Positioned(
                              top: widget.laser.value * 200,
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
                                      color: jade400,
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
                ),
                const Positioned(
                  bottom: 16,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: Color(0xE60D1516),
                      borderRadius: BorderRadius.all(Radius.circular(16)),
                    ),
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 12, vertical: 5),
                      child: Text(
                        '将二维码放入框内',
                        style: TextStyle(color: textPrimary, fontSize: 11),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 14),
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceEvenly,
          children: <Widget>[
            ElevatedButton.icon(
              onPressed: widget.onScanImage,
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
              onPressed: controller == null ? null : _toggleTorch,
              icon: Icon(
                _torchOn ? Icons.flash_on : Icons.flash_off,
                size: 16,
                color: jade400,
              ),
              label: Text(
                _torchOn ? '关闭补光' : '补光灯',
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
      ],
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
