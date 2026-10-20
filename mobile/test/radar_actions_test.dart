import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/models.dart';
import 'package:inkhole_mobile/views/radar_view.dart';

/// 雷达页右上角两个按组的回归测试。
///
/// 背景：刷新按钮以前只是重发一遍对端列表，界面上没有任何变化，用户以为
/// 按钮坏了；设置图标的齿轮齿有一半埋在实心圆里，20px 下糊成墨点。
/// 这里锁住「点得动 + 扫描时转起来 + 扫描中不可连点」三条行为。
void main() {
  Widget build({
    required bool isRefreshing,
    required VoidCallback onRefresh,
    VoidCallback? onSettings,
  }) {
    return MaterialApp(
      home: Scaffold(
        body: RadarView(
          listenPort: 41300,
          identityFingerprint: 'abc123',
          peers: const <PeerView>[],
          selectedPeerInstance: null,
          searching: false,
          transferPercent: 0,
          recentReceived: const <ReceivedFile>[],
          onSelectPeer: (_) {},
          onTapCore: () {},
          onSendFile: () {},
          onOpenWormhole: () {},
          onOpenPairQr: () {},
          onOpenSettings: onSettings ?? () {},
          onRefreshDiscovery: onRefresh,
          onOpenInbox: () {},
          onOpenFile: (_) {},
          isRefreshing: isRefreshing,
        ),
      ),
    );
  }

  testWidgets('点刷新按钮会回调且图标在转', (WidgetTester tester) async {
    int taps = 0;
    await tester.pumpWidget(
      build(isRefreshing: false, onRefresh: () => taps++),
    );
    // 雷达页有常驻动画，不能用 pumpAndSettle。
    await tester.pump(const Duration(milliseconds: 16));

    final Finder button = find.byTooltip('刷新雷达扫描');
    expect(button, findsOneWidget);
    final RotationTransition rotation = tester.widget<RotationTransition>(
      find.descendant(of: button, matching: find.byType(RotationTransition)),
    );
    final double before = rotation.turns.value;
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      tester
          .widget<RotationTransition>(
            find.descendant(of: button, matching: find.byType(RotationTransition)),
          )
          .turns
          .value,
      equals(before),
      reason: '未在扫描时图标不该自转',
    );

    await tester.tap(button);
    await tester.pump(const Duration(milliseconds: 16));
    expect(taps, 1);
  });

  testWidgets('扫描中按钮禁用、图标持续旋转', (WidgetTester tester) async {
    int taps = 0;
    await tester.pumpWidget(
      build(isRefreshing: true, onRefresh: () => taps++),
    );
    await tester.pump(const Duration(milliseconds: 16));

    final Finder button = find.byTooltip('刷新雷达扫描');
    // IconButton 把 Tooltip 建在自身内部,所以按钮是 Tooltip 的祖先。
    final IconButton widget = tester.widget<IconButton>(
      find.ancestor(of: button, matching: find.byType(IconButton)),
    );
    expect(widget.onPressed, isNull, reason: '扫描中必须禁止连点');

    final double first = tester
        .widget<RotationTransition>(
          find.descendant(of: button, matching: find.byType(RotationTransition)),
        )
        .turns
        .value;
    await tester.pump(const Duration(milliseconds: 300));
    final double second = tester
        .widget<RotationTransition>(
          find.descendant(of: button, matching: find.byType(RotationTransition)),
        )
        .turns
        .value;
    expect(second, isNot(equals(first)), reason: '扫描中图标应当在转');
    expect(taps, 0);
  });

  testWidgets('设置按钮可点', (WidgetTester tester) async {
    int opens = 0;
    await tester.pumpWidget(
      build(
        isRefreshing: false,
        onRefresh: () {},
        onSettings: () => opens++,
      ),
    );
    await tester.pump(const Duration(milliseconds: 16));
    await tester.tap(find.byTooltip('墨洞配置'));
    await tester.pump(const Duration(milliseconds: 16));
    expect(opens, 1);
  });
}
