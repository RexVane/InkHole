import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/core/updater.dart';

void main() {
  test('版本比较忽略 v 前缀和位数不齐', () {
    expect(versionIsNewer('v2.0.15', '2.0.14'), isTrue);
    expect(versionIsNewer('2.0.14', 'v2.0.14'), isFalse);
    expect(versionIsNewer('2.1', '2.0.14'), isTrue);
  });

  test('按平台选择应用内下载的安装包', () {
    const String tag = 'v2.0.15';
    final Iterable<String> names = <String>[
      'InkHole-v2.0.15.apk',
      'InkHole-v2.0.15-arm64-v8a.apk',
      'InkHole-v2.0.15-ios.zip',
    ];
    expect(chooseReleaseFileName(tag, names, ios: false), 'InkHole-v2.0.15.apk');
    expect(
      chooseReleaseFileName(tag, names, ios: true),
      'InkHole-v2.0.15-ios.zip',
    );
    expect(
      releaseAssetUrl(tag, 'InkHole-v2.0.15.apk'),
      'https://github.com/RexVane/InkHole/releases/download/v2.0.15/InkHole-v2.0.15.apk',
    );
  });
}
