import 'package:flutter/services.dart';

/// 与原生扫码界面(PortraitCaptureActivity)的桥。
class ScannerChannel {
  ScannerChannel._();

  static const MethodChannel _channel =
      MethodChannel('com.rexvane.inkhole/scanner');

  /// 拉起竖屏扫码;用户取消或平台不支持返回 null。
  /// 相机权限被拒时抛 PlatformException('camera_denied')。
  static Future<String?> scan() async {
    try {
      return await _channel.invokeMethod<String>('scan');
    } on MissingPluginException {
      // 桌面/iOS 调试时没有这个通道，按「未扫到」处理。
      return null;
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
  } else {
    code = value;
  }
  if (code.isEmpty || code.length > 160) return null;
  return code;
}
