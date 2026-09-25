import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/models.dart';

void main() {
  group('SSH 中继配置校验', () {
    const String key = '-----BEGIN OPENSSH PRIVATE KEY-----';
    const String fingerprint = 'SHA256:d8Nq57sE2m/1kR+Qw9xLz3TpYbVcNdUfOgHiJkLmNoP';

    String? check({
      String host = 'relay.example.com',
      int port = 22,
      String user = 'root',
      String fp = fingerprint,
      String privateKey = key,
      int remotePort = 0,
      bool requireFingerprint = true,
    }) =>
        validateSshRelayConfig(
          host: host,
          port: port,
          user: user,
          fingerprint: fp,
          privateKey: privateKey,
          remotePort: remotePort,
          requireFingerprint: requireFingerprint,
        );

    test('齐备时通过', () {
      expect(check(), isNull);
    });

    test('缺字段会被逐条拦下', () {
      expect(check(host: '   '), '请填写服务器地址');
      expect(check(host: 'relay host'), '服务器地址不能包含空格');
      expect(check(port: 0), 'SSH 端口必须在 1-65535 范围内');
      expect(check(port: 65536), 'SSH 端口必须在 1-65535 范围内');
      expect(check(user: '  '), '请填写登录用户');
      expect(check(privateKey: ' '), '请导入 SSH 私钥');
      expect(check(remotePort: 70000), '远端端口必须在 0-65535 范围内');
    });

    test('远端端口 0 表示自动分配，是合法值', () {
      expect(check(remotePort: sshRemotePortAuto), isNull);
      expect(sshRemotePortAuto, 0);
    });

    test('建立中继必须已固定指纹', () {
      // 核心 ssh.listen → validate_relay() → normalize(true)，
      // 指纹为空会直接拒绝启动，所以前端必须提前拦下。
      expect(check(fp: ''), '请先「测试握手」取得并确认主机指纹');
      expect(check(fp: 'd8Nq57sE2m/1kR'), '请先「测试握手」取得并确认主机指纹');
    });

    test('测试握手不要求指纹：指纹正是它的结果', () {
      // 否则会死锁：没有指纹就不让握手，而指纹只能由握手取回。
      expect(check(fp: '', requireFingerprint: false), isNull);
    });

    test('指纹必须是 SHA256: 前缀且字符集受限', () {
      expect(freshHostFingerprint(fingerprint), fingerprint);
      expect(
        freshHostFingerprint('  SHA256:d8Nq57sE2m/1kR+Qw9xLz3TpYbVcNdUfOgHiJkLmNoP  '),
        fingerprint,
      );
      expect(freshHostFingerprint('MD5:aa:bb:cc'), isNull);
      expect(freshHostFingerprint('SHA256:'), isNull);
      expect(freshHostFingerprint('SHA256:short'), isNull);
      expect(freshHostFingerprint('SHA256:${'a' * 65}'), isNull);
      expect(freshHostFingerprint('SHA256:${'a' * 32}!'), isNull);
    });
  });

  group('SSH 对端身份', () {
    test('解析核心返回的 peer 对象', () {
      final SshPeerInfo peer = SshPeerInfo.fromJson(<String, dynamic>{
        'id': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'name': '客厅笔电',
        'instance_id': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'remote_port': 32001,
        'noise_public': 'AAAAC3NzaC1lZDI1NTE5AAAAIKg8QGqV',
        'end_to_end': true,
      });
      expect(peer.name, '客厅笔电');
      expect(peer.remotePort, 32001);
      expect(peer.displayName, '客厅笔电');
      expect(peer.shortId, isNotEmpty);
    });

    test('无名对端回退到短 id，不显示空白', () {
      final SshPeerInfo peer = SshPeerInfo.fromJson(<String, dynamic>{
        'instance_id': 'cccccccccccccccccccccccccccccccc',
        'name': '',
        'remote_port': 32002,
      });
      expect(peer.displayName, peer.shortId);
      expect(peer.displayName, isNotEmpty);
    });

    test('过滤掉没有 instance_id 的脏条目', () {
      final List<SshPeerInfo> peers = SshPeerInfo.listFromJson(<dynamic>[
        <String, dynamic>{'instance_id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'},
        <String, dynamic>{'name': '没有 id'},
        'not-a-map',
      ]);
      expect(peers, hasLength(1));
      expect(peers.single.instanceId, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
    });

    test('非列表输入返回空表而不是抛异常', () {
      expect(SshPeerInfo.listFromJson(null), isEmpty);
      expect(SshPeerInfo.listFromJson('oops'), isEmpty);
    });
  });

  group('握手结果', () {
    test('解析核心返回并带上本端实测耗时', () {
      final SshHandshakeResult result = SshHandshakeResult.fromJson(
        <String, dynamic>{
          'fingerprint': 'SHA256:d8Nq57sE',
          'server_version': 'SSH-2.0-OpenSSH_9.6',
          'confirmed': true,
        },
        elapsed: const Duration(milliseconds: 137),
      );
      expect(result.fingerprint, 'SHA256:d8Nq57sE');
      expect(result.serverLabel, 'SSH-2.0-OpenSSH_9.6');
      expect(result.confirmed, isTrue);
      expect(result.elapsedLabel, '137 ms');
    });

    test('缺字段时有兜底文案，不显示空串', () {
      final SshHandshakeResult result = SshHandshakeResult.fromJson(
        <String, dynamic>{},
        elapsed: Duration.zero,
      );
      expect(result.fingerprint, isEmpty);
      expect(result.confirmed, isFalse);
      expect(result.serverLabel, '未知服务端');
      expect(result.elapsedLabel, '0 ms');
    });
  });

  group('中继状态快照', () {
    test('sessionId 能显式清空，也能被无关字段的更新保留', () {
      const SshRelayStatus idle = SshRelayStatus();
      final SshRelayStatus up =
          idle.copyWith(sessionId: 'ssh-1', connected: true, boundRemotePort: 41300);
      expect(up.sessionId, 'ssh-1');
      expect(up.active, isTrue);
      // 不传 sessionId 时不能被覆盖掉（sentinel 的存在意义）。
      expect(up.copyWith(connected: false).sessionId, 'ssh-1');
      // 显式传 null 才清空。
      expect(up.copyWith(sessionId: null).sessionId, isNull);
      expect(up.copyWith(sessionId: null).active, isFalse);
    });

    test('展示端口优先用核心实际绑定到的值', () {
      const SshRelayStatus requested =
          SshRelayStatus(requestedRemotePort: 41300);
      expect(requested.displayRemotePort, 41300);
      // 0 = 自动分配时，核心回报的才是真实端口。
      const SshRelayStatus automatic = SshRelayStatus(requestedRemotePort: 0);
      expect(automatic.displayRemotePort, 0);
      expect(automatic.copyWith(boundRemotePort: 52001).displayRemotePort, 52001);
    });
  });

  group('展示辅助', () {
    test('短标识保留首尾，短串原样返回', () {
      expect(
        shortenIdentifier('0123456789abcdefghijklmnopqrstuv', head: 4, tail: 4),
        '0123…stuv',
      );
      expect(shortenIdentifier('abc'), 'abc');
      expect(shortenIdentifier(''), '');
      // 长度恰好等于 head+tail+1 时不截断，避免出现全是省略号的怪结果。
      expect(shortenIdentifier('0123456789'), '0123456789');
    });

    test('日志时间戳固定为 HH:MM:SS', () {
      expect(formatClock(DateTime(2026, 9, 25, 9, 5, 3)), '09:05:03');
      expect(formatClock(DateTime(2026, 9, 25, 23, 59, 59)), '23:59:59');
      expect(formatClock(DateTime(2026, 9, 25, 0, 0, 0)), '00:00:00');
    });
  });
}
