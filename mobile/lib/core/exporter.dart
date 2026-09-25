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
  ///
  /// 平台未实现该通道(MissingPluginException)或原生导出失败时返回空
  /// location，由调用方按"文件仍在应用内"提示。绝不能把异常抛给收件流程，
  /// 否则用户每收到一个文件都会看到一次导出失败弹窗。
  static Future<ExportOutcome> export(String path, {String? treeUri}) async {
    try {
      return await _export(path: path, treeUri: treeUri);
    } on MissingPluginException {
      return const ExportOutcome(name: '', location: '');
    } on PlatformException {
      return const ExportOutcome(name: '', location: '');
    }
  }

  static Future<ExportOutcome> _export({
    required String path,
    String? treeUri,
  }) async {
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

  /// 弹系统目录选择器;用户取消或平台不支持返回 null。
  static Future<PickedDirectory?> pickDirectory() async {
    try {
      return await _pickDirectory();
    } on MissingPluginException {
      return null;
    } on PlatformException {
      return null;
    }
  }

  static Future<PickedDirectory?> _pickDirectory() async {
    final Map<Object?, Object?>? raw =
        await _channel.invokeMethod<Map<Object?, Object?>>('pickDirectory');
    if (raw == null) return null;
    final String uri = '${raw['uri'] ?? ''}';
    if (uri.isEmpty) return null;
    return PickedDirectory(uri: uri, label: '${raw['label'] ?? '自定义目录'}');
  }

  /// 打开一条收件记录。返回 [openedExact] 表示直接打开了该文件，
  /// [openedDownloads] 表示只能回退到系统下载管理(文件夹或记录已被移走)。
  static Future<String> open({
    required String path,
    required String name,
    String? treeUri,
  }) async {
    try {
      return await _open(path: path, name: name, treeUri: treeUri);
    } on MissingPluginException {
      return openedDownloads;
    } on PlatformException {
      return openedDownloads;
    }
  }

  static Future<String> _open({
    required String path,
    required String name,
    String? treeUri,
  }) async {
    final String? outcome =
        await _channel.invokeMethod<String>('open', <String, String?>{
      'path': path,
      'name': name,
      'treeUri': (treeUri == null || treeUri.isEmpty) ? null : treeUri,
    });
    return outcome ?? openedDownloads;
  }

  /// 默认收件落点的绝对路径;非 Android 平台返回 null。
  static Future<String?> downloadsPath() async {
    try {
      return await _channel.invokeMethod<String>('downloadsPath');
    } on MissingPluginException {
      return null;
    } on PlatformException {
      return null;
    }
  }

  /// 把 SAF 树 URI 解成可读路径;解不出来返回 null。
  static Future<String?> describeTree(String uri) async {
    if (uri.isEmpty) return null;
    try {
      return await _channel
          .invokeMethod<String>('describeTree', <String, String>{'uri': uri});
    } on MissingPluginException {
      return null;
    } on PlatformException {
      return null;
    }
  }

  static const String openedExact = 'exact';
  static const String openedDownloads = 'downloads';
}
