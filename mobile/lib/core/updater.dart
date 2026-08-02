import 'dart:async';

import 'package:flutter/services.dart';

/// GitHub Release 检查结果。
class UpdateInfo {
  const UpdateInfo({
    required this.version,
    required this.apkUrl,
    required this.notes,
    required this.newer,
  });

  final String version;
  final String apkUrl;
  final String notes;
  final bool newer;
}

/// 与原生 Updater.kt 的桥（检查、应用内下载与安装沿用旧版实现）。
class UpdaterChannel {
  UpdaterChannel._();

  static const MethodChannel _channel =
      MethodChannel('com.rexvane.inkhole/updater');

  static void Function(int percent)? onProgress;
  static bool _handlerInstalled = false;

  static void _ensureHandler() {
    if (_handlerInstalled) return;
    _handlerInstalled = true;
    _channel.setMethodCallHandler((MethodCall call) async {
      if (call.method == 'progress') {
        final int percent = call.arguments is int
            ? call.arguments as int
            : int.tryParse('${call.arguments}') ?? 0;
        onProgress?.call(percent);
      }
      return null;
    });
  }

  /// 查询最新版本；[current] 为当前 appVersion。失败抛 [PlatformException]。
  static Future<UpdateInfo> check(String current) async {
    _ensureHandler();
    final Map<Object?, Object?> raw = await _channel
        .invokeMethod<Map<Object?, Object?>>(
          'check',
          <String, String>{'current': current},
        )
        .then((Map<Object?, Object?>? value) => value ?? <Object?, Object?>{});
    return UpdateInfo(
      version: '${raw['version'] ?? ''}',
      apkUrl: '${raw['apkUrl'] ?? ''}',
      notes: '${raw['notes'] ?? ''}',
      newer: raw['newer'] == true,
    );
  }

  /// 下载并拉起系统安装器；进度经 [onProgress] 回调（0-100）。
  static Future<void> downloadInstall(String url) async {
    _ensureHandler();
    await _channel.invokeMethod<void>(
      'downloadInstall',
      <String, String>{'url': url},
    );
  }
}
