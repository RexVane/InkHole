import 'package:flutter/services.dart';

/// 导出结果:location 为用户可见落点(如 Download/InkHole),空串表示留在应用内。
class ExportOutcome {
  const ExportOutcome({required this.name, required this.location});

  final String name;
  final String location;
}

/// SAF 目录选择结果。
class PickedDirectory {
  const PickedDirectory({required this.uri, required this.label});

  final String uri;
  final String label;
}

/// 与原生 Exporter.kt 的桥:收件导出与自定义目录选择。
class ExporterChannel {
  ExporterChannel._();

  static const MethodChannel _channel =
      MethodChannel('com.rexvane.inkhole/exporter');

  /// 把私有收件箱里的成品导出到公共位置;[treeUri] 为空走默认下载目录。
  static Future<ExportOutcome> export(String path, {String? treeUri}) async {
    final Map<Object?, Object?> raw = await _channel
        .invokeMethod<Map<Object?, Object?>>('export', <String, String?>{
          'path': path,
          'treeUri': treeUri,
        })
        .then((Map<Object?, Object?>? value) => value ?? <Object?, Object?>{});
    return ExportOutcome(
      name: '${raw['name'] ?? ''}',
      location: '${raw['location'] ?? ''}',
    );
  }

  /// 弹系统目录选择器;用户取消返回 null。
  static Future<PickedDirectory?> pickDirectory() async {
    final Map<Object?, Object?>? raw =
        await _channel.invokeMethod<Map<Object?, Object?>>('pickDirectory');
    if (raw == null) return null;
    final String uri = '${raw['uri'] ?? ''}';
    if (uri.isEmpty) return null;
    return PickedDirectory(uri: uri, label: '${raw['label'] ?? '自定义目录'}');
  }
}
