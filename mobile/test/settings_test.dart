import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/core/scanner.dart';
import 'package:inkhole_mobile/models.dart';
import 'package:inkhole_mobile/widgets/settings_dialog.dart';

void main() {
  group('扫码短码解析', () {
    test('认发送端二维码里的 inkhole://receive 链接', () {
      expect(
        parseScannedCode('inkhole://receive?code=7-guitarist-revenge'),
        '7-guitarist-revenge',
      );
      expect(
        parseScannedCode('INKHOLE://RECEIVE?code=3-purple-hydra'),
        '3-purple-hydra',
      );
    });

    test('认直接编码的裸短码', () {
      expect(
          parseScannedCode('  7-guitarist-revenge  '), '7-guitarist-revenge');
    });

    test('认 inkhole: 前缀和接收链接编码', () {
      expect(parseScannedCode('inkhole:7-guitarist-revenge'), '7-guitarist-revenge');
      expect(
        parseScannedCode(wormholeReceiveUri('7-guitarist-revenge')),
        '7-guitarist-revenge',
      );
      expect(looksLikeShortCode('7-guitarist-revenge'), isTrue);
      expect(looksLikeShortCode('inkhole:7-guitarist-revenge'), isFalse);
    });

    test('拒绝无关链接和空内容', () {
      expect(parseScannedCode('https://example.com'), isNull);
      expect(parseScannedCode('inkhole://pair?code=abc'), isNull);
      expect(parseScannedCode('inkhole://receive?code='), isNull);
      expect(parseScannedCode('   '), isNull);
      expect(parseScannedCode('x' * 161), isNull);
    });
  });

  group('手动设备存储格式', () {
    test('备注/主机/端口能原样往返', () {
      const ManualPeer peer =
          ManualPeer(name: '书房台式机', host: '100.64.0.7', port: 41234);
      final ManualPeer decoded = ManualPeer.decode(peer.encode());
      expect(decoded.name, peer.name);
      expect(decoded.host, peer.host);
      expect(decoded.port, peer.port);
    });

    test('QUIC 端口不会被当成发现端口', () {
      final ManualPeer quic = parseManualEndpoint('100.64.0.2:41300', name: 'Tailscale');
      expect(quic.host, '100.64.0.2');
      expect(quic.port, 0);
      expect(discoveryTargetFor(quic), '100.64.0.2');

      final ManualPeer custom = parseManualEndpoint('nas.local:42000');
      expect(custom.port, 42000);
      expect(discoveryTargetFor(custom), 'nas.local:42000');

      final ManualPeer v6 = parseManualEndpoint('[fe80::1]:41300');
      expect(v6.host, 'fe80::1');
      expect(v6.port, 0);
      expect(sameEndpointHost('100.64.0.2:41300', '100.64.0.2'), isTrue);
    });

    test('IPv6 节点往返设置页不会被损坏', () {
      // 设置页把 ManualPeer 渲染成可编辑文本；拼接时必须给 IPv6 加方括号，
      // 否则二次解析会把 `fe80::1:41301` 当成"含多个冒号的主机名"而整串损坏，
      // 存入配置并下发为无效的 discovery_targets。
      final ManualPeer v6 = parseManualEndpoint('[fe80::1]:42000', name: 'Tailscale');
      expect(v6.host, 'fe80::1');
      expect(v6.port, 42000);

      final String rendered = formatManualEndpoint(v6);
      expect(rendered, '[fe80::1]:42000');

      final ManualPeer roundTripped = parseManualEndpoint(rendered, name: 'Tailscale');
      expect(roundTripped.host, 'fe80::1');
      expect(roundTripped.port, 42000);

      // 连往返两轮也要保持一致，避免每次保存都累积污染。
      expect(formatManualEndpoint(roundTripped), rendered);

      // 无端口的 IPv6 只渲染主机，不画蛇添足加括号。
      final ManualPeer bare = parseManualEndpoint('[fe80::1]', name: 'Tailscale');
      expect(bare.port, 0);
      expect(formatManualEndpoint(bare), 'fe80::1');
    });

    test('废弃的明文会合点交给核心默认地址', () {      expect(normalizeRendezvousUrl('ws://relay.magic-wormhole.io:4000/v1'), '');
      expect(normalizeRendezvousUrl(''), '');
      expect(
        normalizeRendezvousUrl('wss://relay.example/v1'),
        'wss://relay.example/v1',
      );
    });

    test('无备注无端口也能往返', () {
      const ManualPeer peer = ManualPeer(name: '', host: 'nas.tail1234.ts.net');
      final ManualPeer decoded = ManualPeer.decode(peer.encode());
      expect(decoded.name, '');
      expect(decoded.host, 'nas.tail1234.ts.net');
      expect(decoded.port, 0);
    });
  });

  group('Tailscale 设备列表', () {
    testWidgets('点过「添加设备」后保存能带回设备', (WidgetTester tester) async {
      InkSettings? saved;
      await tester.pumpWidget(
        _host((InkSettings value) => saved = value, const <ManualPeer>[]),
      );
      await tester.tap(find.text('打开设置'));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('manual-peer-host')),
        '100.64.0.7',
      );
      await tester.ensureVisible(find.text('添加设备'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('添加设备'));
      await tester.pumpAndSettle();
      expect(find.text('100.64.0.7'), findsOneWidget);
      // 加进列表后输入框应当清空，保存时不会再被兜底逻辑重复收一次。
      expect(
        tester
            .widget<TextField>(
              find.byKey(const Key('manual-peer-host')),
            )
            .controller
            ?.text,
        isEmpty,
      );

      await tester.tap(find.widgetWithText(FilledButton, '保存'));
      await tester.pumpAndSettle();
      expect(saved, isNotNull);
      expect(saved!.manualPeers.single.host, '100.64.0.7');
    });

    testWidgets('只填地址直接点保存也不会丢设备', (WidgetTester tester) async {
      InkSettings? saved;
      await tester.pumpWidget(
        _host((InkSettings value) => saved = value, const <ManualPeer>[]),
      );
      await tester.tap(find.text('打开设置'));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('manual-peer-name')),
        '书房台式机',
      );
      await tester.enterText(
        find.byKey(const Key('manual-peer-host')),
        '100.64.0.7',
      );
      await tester.enterText(
        find.byKey(const Key('manual-peer-port')),
        '41234',
      );
      await tester.tap(find.widgetWithText(FilledButton, '保存'));
      await tester.pumpAndSettle();

      expect(saved, isNotNull, reason: '弹窗必须带着草稿关闭');
      expect(saved!.manualPeers, hasLength(1));
      expect(saved!.manualPeers.single.name, '书房台式机');
      expect(saved!.manualPeers.single.host, '100.64.0.7');
      expect(saved!.manualPeers.single.port, 41234);
    });

    testWidgets('地址非法时保存被拦下并给出提示', (WidgetTester tester) async {
      InkSettings? saved;
      await tester.pumpWidget(
        _host((InkSettings value) => saved = value, const <ManualPeer>[]),
      );
      await tester.tap(find.text('打开设置'));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('manual-peer-host')),
        '100.64.0.7 备用',
      );
      await tester.tap(find.widgetWithText(FilledButton, '保存'));
      await tester.pumpAndSettle();

      expect(saved, isNull);
      expect(find.text('Tailscale IP 或 MagicDNS 名称无效'), findsOneWidget);
    });

    testWidgets('已保存的设备重新打开能看到并可编辑删除', (WidgetTester tester) async {
      InkSettings? saved;
      await tester.pumpWidget(
        _host(
          (InkSettings value) => saved = value,
          const <ManualPeer>[
            ManualPeer(name: '书房台式机', host: '100.64.0.7', port: 41234),
          ],
        ),
      );
      await tester.tap(find.text('打开设置'));
      await tester.pumpAndSettle();

      expect(find.text('书房台式机'), findsOneWidget);
      expect(find.text('100.64.0.7:41234'), findsOneWidget);
      expect(find.text('编辑'), findsOneWidget);
      expect(find.text('删除'), findsOneWidget);

      await tester.ensureVisible(find.text('编辑'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('编辑'));
      await tester.pumpAndSettle();
      expect(find.text('保存设备'), findsOneWidget);
      await tester.enterText(
        find.byKey(const Key('manual-peer-host')),
        '100.64.0.9',
      );
      await tester.ensureVisible(find.text('保存设备'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('保存设备'));
      await tester.pumpAndSettle();

      await tester.tap(find.widgetWithText(FilledButton, '保存'));
      await tester.pumpAndSettle();
      expect(saved!.manualPeers.single.host, '100.64.0.9');
    });
  });

  group('存储设置', () {
    testWidgets('显示收件目录的完整路径和应用内暂存路径', (WidgetTester tester) async {
      await tester.pumpWidget(_host((InkSettings _) {}, const <ManualPeer>[]));
      await tester.tap(find.text('打开设置'));
      await tester.pumpAndSettle();

      expect(find.text('/storage/emulated/0/Download/InkHole'), findsOneWidget);
      expect(
        find.text(
          '导出失败时暂存于应用内：'
          '/data/user/0/com.rexvane.inkhole/app_flutter/InkHole',
        ),
        findsOneWidget,
      );
    });
  });
}

/// 承载设置弹窗的最小宿主，保存后把草稿交给 [onSaved]。
Widget _host(void Function(InkSettings) onSaved, List<ManualPeer> peers) {
  return MaterialApp(
    home: Builder(
      builder: (BuildContext context) => TextButton(
        onPressed: () async {
          final InkSettings? result = await showDialog<InkSettings>(
            context: context,
            builder: (BuildContext dialogContext) => SettingsDialog(
              initial: InkSettings(
                peerName: 'Android',
                listenPort: 0,
                encryptionEnabled: false,
                secret: '',
                manualPeers: peers,
                rendezvousUrl: '',
                transitRelay: '',
                sshEnabled: false,
                sshHost: '',
                sshPort: 22,
                sshUser: '',
                sshFingerprint: '',
                sshPrivateKey: '',
                sshPassphrase: '',
              ),
              deviceLine: '本机：Android-1234abcd',
              portLine: '端口：0',
              inboxPath: '/data/user/0/com.rexvane.inkhole/app_flutter/InkHole',
              exportPath: '/storage/emulated/0/Download/InkHole',
              onPickExportDirectory: () async => null,
              onResetExportDirectory: () async =>
                  '/storage/emulated/0/Download/InkHole',
              sshReady: false,
              onCheckSsh: (InkSettings draft) async => null,
              onCreateSshPair: () {},
              onOpenCrossNetwork: () {},
              onOpenRepository: () {},
            ),
          );
          if (result != null) onSaved(result);
        },
        child: const Text('打开设置'),
      ),
    ),
  );
}
