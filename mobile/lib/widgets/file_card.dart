import 'package:flutter/material.dart';

import '../models.dart';
import '../theme.dart';

/// 接收记录卡片，对应旧版 InkHoleUI.kt#FileCard：
/// 表情图标 + 文件名 + 「大小 · 时间」，右侧一个青色的轻操作。
/// 轻点打开文件，长按复制路径。
class FileCard extends StatelessWidget {
  const FileCard({
    super.key,
    required this.file,
    required this.onTap,
    required this.onLongPress,
  });

  final ReceivedFile file;
  final VoidCallback onTap;
  final VoidCallback onLongPress;

  @override
  Widget build(BuildContext context) {
    final String meta = <String>[
      formatBytes(file.size),
      formatRelativeTime(file.receivedAt),
      if (file.sender.isNotEmpty) file.sender,
    ].join(' · ');
    return Material(
      color: inkBgCard,
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        onTap: onTap,
        onLongPress: onLongPress,
        borderRadius: BorderRadius.circular(12),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 11),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: inkBorder),
          ),
          child: Row(
            children: <Widget>[
              Text(fileEmoji(file.name), style: const TextStyle(fontSize: 18)),
              const SizedBox(width: 11),
              Expanded(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(
                      file.name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: inkTextPrimary,
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      meta,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: inkTextDim, fontSize: 10.5),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Text(
                '打开',
                style: TextStyle(
                  color: inkTeal.withValues(alpha: 0.8),
                  fontSize: 11,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// 按扩展名挑表情，照抄旧版 fileEmoji()。
String fileEmoji(String name) {
  final int dot = name.lastIndexOf('.');
  if (dot < 0 || dot == name.length - 1) return '📦';
  switch (name.substring(dot + 1).toLowerCase()) {
    case 'jpg':
    case 'jpeg':
    case 'png':
    case 'gif':
    case 'webp':
    case 'heic':
    case 'bmp':
      return '🖼️';
    case 'mp4':
    case 'mov':
    case 'mkv':
    case 'avi':
    case 'webm':
      return '🎬';
    case 'mp3':
    case 'flac':
    case 'wav':
    case 'm4a':
    case 'ogg':
      return '🎵';
    case 'pdf':
      return '📕';
    case 'doc':
    case 'docx':
    case 'txt':
    case 'md':
      return '📄';
    case 'xls':
    case 'xlsx':
    case 'csv':
      return '📊';
    case 'ppt':
    case 'pptx':
      return '📽️';
    case 'zip':
    case 'rar':
    case '7z':
    case 'tar':
    case 'gz':
      return '🗜️';
    case 'apk':
      return '🤖';
    case 'py':
    case 'js':
    case 'ts':
    case 'kt':
    case 'java':
    case 'c':
    case 'cpp':
    case 'go':
    case 'rs':
    case 'html':
    case 'css':
      return '💻';
    default:
      return '📦';
  }
}
