import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

/// GitHub Release 检查结果。
class UpdateInfo {
  const UpdateInfo({
    required this.version,
    required this.apkUrl,
    required this.notes,
    required this.newer,
    this.fileName = '',
  });

  final String version;
  final String apkUrl;
  final String notes;
  final bool newer;
  final String fileName;
}

const String releaseRepository = 'RexVane/InkHole';
const String releaseApi =
    'https://api.github.com/repos/$releaseRepository/releases/latest';
const String releasePage =
    'https://github.com/$releaseRepository/releases/latest';
const int _maxReleaseBytes = 250 * 1024 * 1024;

/// remote 是否比 local 新。容忍 `v` 前缀和位数不齐。
bool versionIsNewer(String remote, String local) {
  List<int> parts(String value) {
    final List<int> parsed = value
        .trim()
        .replaceFirst(RegExp(r'^[vV]'), '')
        .split('.')
        .map((String segment) {
          final String digits = segment.replaceAll(RegExp(r'[^0-9]'), '');
          return int.tryParse(digits) ?? 0;
        })
        .toList(growable: false);
    if (parsed.length >= 4) return parsed;
    return <int>[...parsed, ...List<int>.filled(4 - parsed.length, 0)];
  }

  final List<int> left = parts(remote);
  final List<int> right = parts(local);
  final int count = left.length > right.length ? left.length : right.length;
  for (var index = 0; index < count; index++) {
    final int x = index < left.length ? left[index] : 0;
    final int y = index < right.length ? right[index] : 0;
    if (x != y) return x > y;
  }
  return false;
}

/// 按当前平台从 Release 资源里挑要下载的文件。
///
/// Android 优先通用包（arm64 别名），iOS 用 `InkHole-<tag>-ios.zip`。
String? chooseReleaseFileName(
  String tag,
  Iterable<String> names, {
  required bool ios,
}) {
  final Set<String> available = names.toSet();
  if (ios) {
    final String zip = 'InkHole-$tag-ios.zip';
    return available.contains(zip) ? zip : null;
  }
  final String universal = 'InkHole-$tag.apk';
  if (available.contains(universal)) return universal;
  final String arm64 = 'InkHole-$tag-arm64-v8a.apk';
  if (available.contains(arm64)) return arm64;
  for (final String name in available) {
    if (name.startsWith('InkHole-$tag-') && name.endsWith('.apk')) return name;
  }
  return null;
}

String releaseAssetUrl(String tag, String fileName) =>
    'https://github.com/$releaseRepository/releases/download/$tag/$fileName';

String _safeReleaseFileName(String name) {
  final String base = p.basename(name);
  if (!RegExp(r'^InkHole-[A-Za-z0-9._-]+$').hasMatch(base)) {
    throw StateError('更新文件名无效');
  }
  return base;
}

/// 不经过浏览器，直接把 Release 资源下载到应用文档目录。
class ReleaseDownloader {
  const ReleaseDownloader();

  Future<UpdateInfo> fetch(String current) async {
    try {
      return await _fromApi(current);
    } catch (_) {
      return _fromRedirect(current);
    }
  }

  Future<File> download(
    UpdateInfo info,
    void Function(int percent) onProgress,
  ) async {
    final Uri uri = Uri.parse(info.apkUrl);
    if (uri.scheme != 'https') {
      throw StateError('更新地址不是 HTTPS');
    }
    final String fileName = _safeReleaseFileName(
      info.fileName.isEmpty ? uri.pathSegments.last : info.fileName,
    );
    final Directory root = await getApplicationDocumentsDirectory();
    final Directory folder = Directory(p.join(root.path, 'updates'));
    await folder.create(recursive: true);
    final File destination = File(p.join(folder.path, fileName));
    final HttpClient client = HttpClient();
    IOSink? sink;
    try {
      final HttpClientRequest request = await client.getUrl(uri);
      request.followRedirects = true;
      request.headers.set(HttpHeaders.userAgentHeader, 'InkHole-Updater');
      final HttpClientResponse response =
          await request.close().timeout(const Duration(seconds: 30));
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw HttpException(
          '下载服务器返回 HTTP ${response.statusCode}',
          uri: uri,
        );
      }
      final int total = response.contentLength;
      if (total > _maxReleaseBytes) throw StateError('安装包大小异常');
      sink = destination.openWrite();
      var received = 0;
      await for (final List<int> chunk in response.timeout(const Duration(seconds: 30))) {
        received += chunk.length;
        if (received > _maxReleaseBytes) throw StateError('安装包大小异常');
        sink.add(chunk);
        if (total > 0) onProgress((received * 100) ~/ total);
      }
      await sink.flush();
      await sink.close();
      sink = null;
      if (received == 0) throw StateError('下载内容为空');
      onProgress(100);
      return destination;
    } catch (error) {
      await sink?.close();
      if (await destination.exists()) await destination.delete();
      rethrow;
    } finally {
      client.close(force: true);
    }
  }

  Future<UpdateInfo> _fromApi(String current) async {
    final Map<String, dynamic> body = await _getJson(Uri.parse(releaseApi));
    final String tag = '${body['tag_name'] ?? ''}'.trim();
    if (tag.isEmpty) throw StateError('最新版本号为空');
    final Map<String, String> assets = <String, String>{};
    final Object? rawAssets = body['assets'];
    if (rawAssets is List) {
      for (final Object? item in rawAssets) {
        if (item is! Map) continue;
        final String name = '${item['name'] ?? ''}'.trim();
        final String url = '${item['browser_download_url'] ?? ''}'.trim();
        if (name.isEmpty || url.isEmpty) continue;
        assets[name] = url;
      }
    }
    return _info(tag, '${body['body'] ?? ''}', current, assets);
  }

  Future<UpdateInfo> _fromRedirect(String current) async {
    final HttpClient client = HttpClient();
    try {
      final HttpClientRequest request = await client.getUrl(Uri.parse(releasePage));
      request.followRedirects = false;
      request.headers.set(HttpHeaders.userAgentHeader, 'InkHole-Updater');
      final HttpClientResponse response =
          await request.close().timeout(const Duration(seconds: 20));
      final String location = response.headers.value(HttpHeaders.locationHeader) ?? '';
      await response.drain<void>();
      final String tag = location.trim().replaceAll(RegExp(r'/+$'), '').split('/tag/').last;
      if (tag.isEmpty || tag.contains('/')) throw StateError('无法解析最新版本号');
      return _info(tag, '', current, const <String, String>{});
    } finally {
      client.close(force: true);
    }
  }

  UpdateInfo _info(
    String tag,
    String notes,
    String current,
    Map<String, String> assets,
  ) {
    final String? fileName = chooseReleaseFileName(
      tag,
      assets.keys,
      ios: Platform.isIOS,
    );
    if (assets.isNotEmpty && fileName == null) {
      return UpdateInfo(
        version: tag,
        apkUrl: '',
        notes: _summarize(notes),
        newer: versionIsNewer(tag, current),
      );
    }
    final String resolvedName = fileName ??
        (Platform.isIOS ? 'InkHole-$tag-ios.zip' : 'InkHole-$tag.apk');
    final String url = assets[resolvedName] ?? releaseAssetUrl(tag, resolvedName);
    return UpdateInfo(
      version: tag,
      apkUrl: url,
      notes: _summarize(notes),
      newer: versionIsNewer(tag, current),
      fileName: resolvedName,
    );
  }

  Future<Map<String, dynamic>> _getJson(Uri uri) async {
    final HttpClient client = HttpClient();
    try {
      final HttpClientRequest request = await client.getUrl(uri);
      request.followRedirects = true;
      request.headers.set(HttpHeaders.userAgentHeader, 'InkHole-Updater');
      request.headers.set(HttpHeaders.acceptHeader, 'application/vnd.github+json');
      final HttpClientResponse response =
          await request.close().timeout(const Duration(seconds: 20));
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw HttpException('GitHub 返回 HTTP ${response.statusCode}', uri: uri);
      }
      final String text = await utf8.decoder.bind(response).join();
      final Object? decoded = jsonDecode(text);
      if (decoded is! Map) throw StateError('更新信息格式无效');
      return Map<String, dynamic>.from(decoded);
    } finally {
      client.close(force: true);
    }
  }

  String _summarize(String raw) {
    final List<String> items = <String>[];
    for (final String rawLine in raw.replaceAll('\r\n', '\n').split('\n')) {
      final String line = rawLine.trim();
      if (line.isEmpty || line.startsWith('#') || line.startsWith('>')) continue;
      String text = line
          .replaceAll(RegExp(r'^[-*+]\s+'), '')
          .replaceAll(RegExp(r'^\d+[.)]\s+'), '')
          .replaceAll('**', '')
          .replaceAll('`', '')
          .trim();
      if (text.isEmpty || text.startsWith('http://') || text.startsWith('https://')) {
        continue;
      }
      if (text.length > 90) text = '${text.substring(0, 89).trimRight()}…';
      items.add('• $text');
      if (items.length >= 4) break;
    }
    return items.join('\n');
  }
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
