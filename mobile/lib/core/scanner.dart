import 'dart:io';

import 'package:flutter/services.dart';

/// 与原生扫码界面的桥。
///
/// Android 走旧的 zxing 全屏取景(MainActivity 的 scanner 通道)；iOS 端
/// 真实取景由 mobile_scanner 插件自己完成，本通道只负责"相册识码"。
class ScannerChannel {
  ScannerChannel._();

  static const MethodChannel _channel =
      MethodChannel('com.rexvane.inkhole/scanner');

  /// 当前平台是否支持"从相册选图识码"。
  ///
  /// iOS 侧尚未实现该通道，调用只会拿到 null——UI 必须据此隐藏入口，
  /// 不能让用户点了毫无反应。
  static bool get supportsImageScan => !Platform.isIOS;

  /// 拉起竖屏扫码;用户取消或平台不支持返回 null。
  /// 相机权限被拒时抛 PlatformException('camera_denied')。
  /// [torch] 为 true 时取景界面打开后即点亮补光灯。
  static Future<String?> scan({bool torch = false}) async {
    try {
      return await _channel.invokeMethod<String>('scan', <String, bool>{
        'torch': torch,
      });
    } on MissingPluginException {
      // 桌面/iOS 调试时没有这个通道，按「未扫到」处理。
      return null;
    }
  }

  /// 从相册图片里解一次短码。没有二维码时抛 PlatformException('scan_not_found')。
  ///
  /// 平台未实现该通道时抛 [UnsupportedError]，而不是静默返回 null——
  /// 否则 UI 无法区分"用户没扫到"与"平台不支持"。
  static Future<String?> scanImage() async {
    try {
      return await _channel.invokeMethod<String>('scanImage');
    } on MissingPluginException {
      throw UnsupportedError('当前平台不支持从相册识码');
    }
  }
}

/// 把扫到的内容归一成一次性短码。
///
/// 既认发送端二维码里的 `inkhole://receive?code=xxx`，也认直接编码的裸短码；
/// 其它链接一律拒绝，避免把随手扫到的网址塞进输入框。规则与旧版
/// MainActivity.kt#handleScannedReceiveCode 一致。
String? parseScannedCode(String raw) {
  final String value = raw.trim();
  if (value.isEmpty) return null;
  final Uri? uri = Uri.tryParse(value);
  final bool receiveUri = uri != null &&
      uri.scheme.toLowerCase() == 'inkhole' &&
      uri.host.toLowerCase() == 'receive';
  final String code;
  if (receiveUri) {
    code = (uri.queryParameters['code'] ?? '').trim();
  } else if (value.contains('://')) {
    code = '';
  } else if (value.toLowerCase().startsWith('inkhole:')) {
    code = value.substring(value.indexOf(':') + 1).trim();
  } else {
    code = value;
  }
  if (code.isEmpty || code.length > 160 || code.contains('://')) return null;
  return code;
}
