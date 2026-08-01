import 'package:flutter/material.dart';

import '../theme.dart';

/// 使用说明，对应旧版 MainActivity.kt#UsageGuideDialog（首次启动自动弹一次）。
Future<void> showUsageGuide(BuildContext context) {
  return showDialog<void>(
    context: context,
    builder: (BuildContext dialogContext) => const _UsageGuideDialog(),
  );
}

class _UsageGuideDialog extends StatelessWidget {
  const _UsageGuideDialog();

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: inkBgCard,
      title: const Text('使用说明', style: TextStyle(color: inkTextPrimary)),
      content: const SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            _GuideSection(
              '局域网',
              '两台设备连接同一个 WiFi，并同时打开墨洞。发现设备后，点击对方设备，'
                  '再轻点墨洞选择文件发送。',
            ),
            _GuideSection(
              '跨网络',
              '长期直连可在设置里填对方的 Tailscale 地址；临时发送可从首页跨网按钮'
                  '生成一次性短码；有 VPS 时可在设置中启用 SSH 中继，并用配对码添加长期设备。',
            ),
            Text(
              '收到的文件保存在应用收件箱，也可以在首页「已接收」中查看。',
              style: TextStyle(color: inkTextDim, fontSize: 12),
            ),
          ],
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('知道了'),
        ),
      ],
    );
  }
}

class _GuideSection extends StatelessWidget {
  const _GuideSection(this.title, this.body);

  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            title,
            style: const TextStyle(
              color: inkTextPrimary,
              fontSize: 14,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            body,
            style: const TextStyle(color: inkTextDim, fontSize: 12),
          ),
        ],
      ),
    );
  }
}
