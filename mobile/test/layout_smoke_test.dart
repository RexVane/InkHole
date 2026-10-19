import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/main.dart';
import 'package:inkhole_mobile/widgets/bottom_nav_bar.dart';

/// 布局冒烟测试：把 5 个 Tab 在窄屏与大字体下各打开一次。
///
/// 这类回归静态分析抓不到——`Row` 里少一个 `Expanded` 在 analyze 里完全合法，
/// 只有真正布局到具体宽度才会画溢出条纹（debug 包直接显示黄黑条，
/// release 包表现为右侧内容被裁掉）。历史上收件箱 / 设置 / 配对各有一处。
///
/// 注意：雷达页有常驻循环动画，必须用 `pump(duration)`，不能用
/// `pumpAndSettle`，否则永远等不到静止帧。
void main() {
  const List<String> tabs = <String>['探索', '传输', '收件箱', '配对', '设置'];

  Future<void> pumpApp(WidgetTester tester) async {
    await tester.pumpWidget(const InkHoleApp());
    await tester.pump(const Duration(milliseconds: 50));
    // 首帧也要断言：探索页在这一帧就已经布局，溢出必须当场暴露。
    expect(tester.takeException(), isNull, reason: '首帧（探索页）布局异常');
  }

  Future<void> openTab(WidgetTester tester, String label) async {
    await tester.tap(
      find.descendant(
        of: find.byType(CyberBottomNavBar),
        matching: find.text(label),
      ),
    );
    await tester.pump(const Duration(milliseconds: 120));
    expect(
      tester.takeException(),
      isNull,
      reason: '「$label」页在该尺寸下布局溢出或抛异常',
    );
  }

  const Map<String, Size> viewports = <String, Size>{
    '320x640': Size(320, 640),
    '360x640': Size(360, 640),
    '390x844': Size(390, 844),
  };

  for (final MapEntry<String, Size> viewport in viewports.entries) {
    testWidgets('${viewport.key} 五个 Tab 无布局异常', (WidgetTester tester) async {
      tester.view.physicalSize = viewport.value;
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);

      await pumpApp(tester);
      for (final String tab in tabs) {
        await openTab(tester, tab);
      }
    });
  }

  // 系统字体放大是真实用户最常见的“把布局撑破”的开关，单独覆盖一档。
  testWidgets('390x844 字体放大 1.3 倍仍无溢出', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    tester.platformDispatcher.textScaleFactorTestValue = 1.3;
    addTearDown(tester.view.reset);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);

    await pumpApp(tester);
    for (final String tab in tabs) {
      await openTab(tester, tab);
    }
  });
}
