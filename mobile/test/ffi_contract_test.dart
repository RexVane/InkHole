import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/core/inkhole_core.dart';

/// 锁死 FFI 契约三方一致：Dart 常量 ↔ C 头文件 ↔ Rust 导出。
///
/// 项目约定（AGENTS.md）：`crates/inkhole-ffi/src/lib.rs` 的状态码必须与
/// `include/inkhole.h` 及 `mobile/lib/core/inkhole_core.dart` 严格一致。
/// 以前只靠注释提醒，改一边漏另一边时表现为“状态码 3 被当成 invalid utf8 之外
/// 的东西”这类难以定位的错位，所以在 Dart 侧加一条测试把差异挡住。
void main() {
  /// 从 `mobile/` 往上找到含 `rust/crates/inkhole-ffi` 的仓库根。
  Directory? findRepoRoot() {
    Directory current = Directory.current.absolute;
    for (int depth = 0; depth < 4; depth++) {
      final Directory candidate =
          Directory('${current.path}/rust/crates/inkhole-ffi');
      if (candidate.existsSync()) return current;
      if (current.parent.path == current.path) break;
      current = current.parent;
    }
    return null;
  }

  final Directory? root = findRepoRoot();
  final Map<String, int> expected = <String, int>{
    'INKHOLE_STATUS_OK': inkholeStatusOk,
    'INKHOLE_STATUS_NO_EVENT': inkholeStatusNoEvent,
    'INKHOLE_STATUS_INVALID_ARGUMENT': inkholeStatusInvalidArgument,
    'INKHOLE_STATUS_INVALID_UTF8': inkholeStatusInvalidUtf8,
    'INKHOLE_STATUS_CLOSED': inkholeStatusClosed,
    'INKHOLE_STATUS_INTERNAL_ERROR': inkholeStatusInternalError,
    'INKHOLE_STATUS_PANIC': inkholeStatusPanic,
    'INKHOLE_STATUS_WRONG_THREAD': inkholeStatusWrongThread,
    'INKHOLE_FFI_ABI_VERSION': inkholeFfiAbiVersion,
  };

  /// 从源码里抓 `NAME` 的整数值：兼容 `#define NAME 0` 与
  /// `pub const NAME: i32 = 0;` 两种写法，末尾的 `u` 后缀可有可无。
  int? readValue(String source, String name) {
    final RegExp pattern = RegExp(
      '(?:#define|pub\\s+const)\\s+$name\\s*(?::\\s*\\w+)?\\s*(?:=|\\s)\\s*(\\d+)u?',
    );
    final Match? match = pattern.firstMatch(source);
    return match == null ? null : int.parse(match.group(1)!);
  }

  test('状态码与 ABI 版本在 C 头文件中一致', () {
    final File header =
        File('${root?.path}/rust/crates/inkhole-ffi/include/inkhole.h');
    if (root == null || !header.existsSync()) {
      markTestSkipped('缺少 rust/crates/inkhole-ffi/include/inkhole.h');
      return;
    }
    final String source = header.readAsStringSync();
    for (final MapEntry<String, int> entry in expected.entries) {
      expect(
        readValue(source, entry.key),
        entry.value,
        reason: '${entry.key} 在 inkhole.h 中缺失或与 Dart 侧不一致',
      );
    }
  });

  test('状态码与 ABI 版本在 Rust 导出中一致', () {
    final File lib = File('${root?.path}/rust/crates/inkhole-ffi/src/lib.rs');
    if (root == null || !lib.existsSync()) {
      markTestSkipped('缺少 rust/crates/inkhole-ffi/src/lib.rs');
      return;
    }
    final String source = lib.readAsStringSync();
    for (final MapEntry<String, int> entry in expected.entries) {
      expect(
        readValue(source, entry.key),
        entry.value,
        reason: '${entry.key} 在 inkhole-ffi/src/lib.rs 中缺失或与 Dart 侧不一致',
      );
    }
  });
}
