import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/home_page.dart';

/// 收件目录里的内部文件必须被排除在收件列表之外。
///
/// 真机上踩到的坑：核心把断点续传状态放在 `<inbox>/.inkhole-transfers/`，
/// 文件名是 transfer_id（`680fdf5c-….json`、`.part`）。「重扫收件目录」如果
/// 不排除它们，用户会在收件箱里看到两条 545 字节的 UUID 记录，"清空收件"
/// 还会顺手删掉正在续传所需的状态文件。
void main() {
  test('核心的断点状态目录不算收件', () {
    expect(
      isInboxInternalFile(
        '/data/user/0/com.rexvane.inkhole/app_flutter/InkHole/'
        '.inkhole-transfers/680fdf5c-c831-4a12-b507-948360b90419.json',
      ),
      isTrue,
    );
    expect(
      isInboxInternalFile('/data/user/0/inbox/.inkhole-transfers/abc.part'),
      isTrue,
    );
  });

  test('隐藏目录下的任何文件都不算收件', () {
    expect(isInboxInternalFile('/data/user/0/inbox/.trash/photo.jpg'), isTrue);
  });

  test('临时后缀不算收件', () {
    expect(isInboxInternalFile('/data/user/0/inbox/holiday.jpg.part'), isTrue);
    expect(isInboxInternalFile('/data/user/0/inbox/x.metadata'), isTrue);
    expect(isInboxInternalFile('/data/user/0/inbox/x.commit'), isTrue);
  });

  test('正常成品文件与子目录里的文件算收件', () {
    expect(
      isInboxInternalFile('/data/user/0/inbox/holiday.jpg'),
      isFalse,
    );
    expect(
      isInboxInternalFile('/data/user/0/inbox/文档/年度报告.pdf'),
      isFalse,
    );
    expect(
      isInboxInternalFile('/data/user/0/inbox/相册/2026/IMG_0001.HEIC'),
      isFalse,
    );
  });
}
