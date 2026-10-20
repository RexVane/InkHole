import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/home_page.dart';
import 'package:inkhole_mobile/theme.dart';

/// 「清空收件」必须先确认，且取消返回 false。
///
/// 背景：这个动作以前只清本地历史、不删文件、也不问一句；改成连文件一起删
/// 之后，误触的代价从"记录没了"变成"文件没了"，确认框成了硬需求。
///
/// 这里直接测确认框本身，不启动整个 App：`_boot` 会摸 path_provider 与
/// 安全存储，在 widget 测试里跑不通。
void main() {
  Future<bool?> pumpAndTap(WidgetTester tester, String button) async {
    bool? result;
    await tester.pumpWidget(
      MaterialApp(
        theme: buildInkHoleTheme(),
        home: Scaffold(
          body: Builder(
            builder: (BuildContext context) => Center(
              child: ElevatedButton(
                onPressed: () async {
                  result = await confirmClearReceivedFiles(context, 3);
                },
                child: const Text('start'),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.tap(find.text('start'));
    await tester.pump(const Duration(milliseconds: 250));
    expect(find.text('清空收件'), findsOneWidget);
    expect(
      find.textContaining('不可恢复'),
      findsOneWidget,
      reason: '必须明确告知不可恢复',
    );
    expect(
      find.textContaining('3 个文件'),
      findsOneWidget,
      reason: '要报出将删除的条数',
    );
    await tester.tap(find.text(button));
    await tester.pump(const Duration(milliseconds: 250));
    return result;
  }

  testWidgets('点取消返回 false 并关闭对话框', (WidgetTester tester) async {
    final bool? confirmed = await pumpAndTap(tester, '取消');
    expect(confirmed, isFalse);
    expect(find.text('清空收件'), findsNothing);
  });

  testWidgets('点删除返回 true', (WidgetTester tester) async {
    final bool? confirmed = await pumpAndTap(tester, '删除');
    expect(confirmed, isTrue);
    expect(find.text('清空收件'), findsNothing);
  });
}
