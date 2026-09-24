import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../models.dart';
import '../theme.dart';

/// Tab 2: 收件仓库与文件流视图 (Inbox & Storage Stream)
class InboxView extends StatefulWidget {
  const InboxView({
    super.key,
    required this.inboxPath,
    required this.files,
    required this.onOpenFile,
    required this.onBrowseDirectory,
    required this.onClearAll,
    required this.onRefresh,
    required this.onOpenPairQr,
    required this.onOpenSettings,
  });

  final String inboxPath;
  final List<ReceivedFile> files;
  final ValueChanged<ReceivedFile> onOpenFile;
  final VoidCallback onBrowseDirectory;
  final VoidCallback onClearAll;
  final VoidCallback onRefresh;
  final VoidCallback onOpenPairQr;
  final VoidCallback onOpenSettings;

  @override
  State<InboxView> createState() => _InboxViewState();
}

class _InboxViewState extends State<InboxView> {
  int _selectedCategory = 0; // 0=全部, 1=文档, 2=媒体, 3=代码, 4=压缩包

  List<ReceivedFile> get _filteredFiles {
    if (_selectedCategory == 0) return widget.files;

    return widget.files.where((ReceivedFile f) {
      final String ext = f.name.toLowerCase();
      if (_selectedCategory == 1) {
        return ext.endsWith('.pdf') ||
            ext.endsWith('.doc') ||
            ext.endsWith('.docx') ||
            ext.endsWith('.txt') ||
            ext.endsWith('.md');
      }
      if (_selectedCategory == 2) {
        return ext.endsWith('.png') ||
            ext.endsWith('.jpg') ||
            ext.endsWith('.jpeg') ||
            ext.endsWith('.mp4') ||
            ext.endsWith('.mov') ||
            ext.endsWith('.mp3');
      }
      if (_selectedCategory == 3) {
        return ext.endsWith('.rs') ||
            ext.endsWith('.dart') ||
            ext.endsWith('.go') ||
            ext.endsWith('.py') ||
            ext.endsWith('.bin') ||
            ext.endsWith('.json');
      }
      if (_selectedCategory == 4) {
        return ext.endsWith('.zip') ||
            ext.endsWith('.tar') ||
            ext.endsWith('.gz') ||
            ext.endsWith('.zst') ||
            ext.endsWith('.apk');
      }
      return true;
    }).toList();
  }

  int get _totalBytes =>
      widget.files.fold<int>(0, (int sum, ReceivedFile f) => sum + f.size);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      appBar: AppBar(
        title: Row(
          children: <Widget>[
            const Text(
              '墨洞',
              style: TextStyle(
                color: textPrimary,
                fontSize: 20,
                fontWeight: FontWeight.w900,
                letterSpacing: 1.2,
              ),
            ),
            const SizedBox(width: 6),
            Text(
              'InkHole',
              style: TextStyle(
                color: jade400.withValues(alpha: 0.9),
                fontSize: 12,
                fontFamily: 'monospace',
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
        actions: <Widget>[
          IconButton(
            onPressed: widget.onRefresh,
            icon: const Icon(Icons.refresh, size: 21),
            color: textMuted,
            splashRadius: 18,
          ),
          IconButton(
            onPressed: widget.onOpenPairQr,
            icon: const Icon(Icons.language, size: 21),
            color: textMuted,
            splashRadius: 18,
          ),
          IconButton(
            onPressed: widget.onOpenSettings,
            icon: const Icon(Icons.settings_outlined, size: 21),
            color: textMuted,
            splashRadius: 18,
          ),
        ],
      ),
      body: SingleChildScrollView(
        physics: const BouncingScrollPhysics(),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            // 1. 存储与路径遥测控制台
            _buildStorageConsole(context),
            const SizedBox(height: 14),

            // 2. 分类筛选胶囊栏
            _buildCategoryFilterRibbon(),
            const SizedBox(height: 14),

            // 3. 接收文件流
            _buildFileStreamSection(context),
            const SizedBox(height: 14),

            // 4. 底部批量控制条
            _buildBatchControlBar(context),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }

  Widget _buildStorageConsole(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: borderLuminescent),
        boxShadow: const <BoxShadow>[
          BoxShadow(
            color: Color(0x66000000),
            blurRadius: 18,
            offset: Offset(0, 4),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          // 标题与浏览目录按钮
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Container(
                    width: 4,
                    height: 14,
                    decoration: BoxDecoration(
                      color: jade400,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                  const SizedBox(width: 8),
                  const Text(
                    '收件仓库',
                    style: TextStyle(
                      color: textPrimary,
                      fontSize: 16,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: surfaceActive,
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: const Text(
                      'LOCAL-STORAGE',
                      style: TextStyle(
                        color: jade400,
                        fontSize: 9,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
              InkWell(
                onTap: widget.onBrowseDirectory,
                borderRadius: BorderRadius.circular(16),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                  decoration: BoxDecoration(
                    color: surfaceActive,
                    borderRadius: BorderRadius.circular(16),
                    border: Border.all(color: borderLuminescent),
                  ),
                  child: const Row(
                    children: <Widget>[
                      Icon(Icons.folder_open, color: jade400, size: 14),
                      SizedBox(width: 4),
                      Text(
                        '浏览目录',
                        style: TextStyle(
                          color: jade400,
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),

          // 路径终端小框
          GestureDetector(
            onTap: () {
              Clipboard.setData(ClipboardData(text: widget.inboxPath));
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(
                  content: Text('收件路径已复制到剪贴板'),
                  duration: Duration(seconds: 2),
                ),
              );
            },
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              decoration: BoxDecoration(
                color: surfaceLowest,
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: surfaceBorder),
              ),
              child: Row(
                children: <Widget>[
                  const Icon(Icons.terminal, color: textMuted, size: 14),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      widget.inboxPath.isNotEmpty
                          ? widget.inboxPath
                          : '/storage/emulated/0/Download/InkHole',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: textMuted,
                        fontSize: 11,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ),
                  const Icon(Icons.copy, color: textDim, size: 13),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),

          // 容量统计
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Text(
                    '${widget.files.length} 项归档',
                    style: const TextStyle(
                      color: textPrimary,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const Text(' · ', style: TextStyle(color: textDim, fontSize: 11)),
                  Text(
                    '占用 ${formatBytes(_totalBytes)}',
                    style: const TextStyle(color: textMuted, fontSize: 11),
                  ),
                ],
              ),
              const Text(
                '剩余空间充足',
                style: TextStyle(color: textDim, fontSize: 11),
              ),
            ],
          ),
          const SizedBox(height: 6),

          // 容量进度条
          Container(
            height: 4,
            width: double.infinity,
            decoration: BoxDecoration(
              color: surfaceLowest,
              borderRadius: BorderRadius.circular(2),
            ),
            child: FractionallySizedBox(
              alignment: Alignment.centerLeft,
              widthFactor: 0.12,
              child: Container(
                decoration: BoxDecoration(
                  color: jade400,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildCategoryFilterRibbon() {
    final List<Map<String, dynamic>> categories = <Map<String, dynamic>>[
      <String, dynamic>{'label': '全部', 'count': widget.files.length},
      <String, dynamic>{'label': '文档', 'icon': Icons.description},
      <String, dynamic>{'label': '媒体', 'icon': Icons.perm_media},
      <String, dynamic>{'label': '代码归档', 'icon': Icons.code},
      <String, dynamic>{'label': '安装与压缩', 'icon': Icons.archive},
    ];

    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      physics: const BouncingScrollPhysics(),
      child: Row(
        children: List<Widget>.generate(categories.length, (int index) {
          final bool active = _selectedCategory == index;
          final Map<String, dynamic> item = categories[index];

          return Padding(
            padding: const EdgeInsets.only(right: 8),
            child: GestureDetector(
              onTap: () => setState(() => _selectedCategory = index),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 160),
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
                decoration: BoxDecoration(
                  color: active ? jade400 : surfaceContainer,
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(
                    color: active ? jade400 : surfaceBorder,
                  ),
                  boxShadow: active
                      ? <BoxShadow>[
                          BoxShadow(
                            color: jade400.withValues(alpha: 0.3),
                            blurRadius: 8,
                          ),
                        ]
                      : null,
                ),
                child: Row(
                  children: <Widget>[
                    if (item['icon'] != null) ...<Widget>[
                      Icon(
                        item['icon'] as IconData,
                        size: 13,
                        color: active ? bgAbyss : textMuted,
                      ),
                      const SizedBox(width: 4),
                    ],
                    Text(
                      item['label'] as String,
                      style: TextStyle(
                        color: active ? bgAbyss : textMuted,
                        fontSize: 12,
                        fontWeight: active ? FontWeight.w700 : FontWeight.w500,
                      ),
                    ),
                    if (item['count'] != null) ...<Widget>[
                      const SizedBox(width: 4),
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                        decoration: BoxDecoration(
                          color: active
                              ? bgAbyss.withValues(alpha: 0.15)
                              : surfaceActive,
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: Text(
                          '${item['count']}',
                          style: TextStyle(
                            color: active ? bgAbyss : jade400,
                            fontSize: 9,
                            fontFamily: 'monospace',
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          );
        }),
      ),
    );
  }

  Widget _buildFileStreamSection(BuildContext context) {
    final List<ReceivedFile> list = _filteredFiles;

    if (list.isEmpty) {
      return Container(
        padding: const EdgeInsets.symmetric(vertical: 40),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: surfaceContainer,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: surfaceBorder),
        ),
        child: const Column(
          children: <Widget>[
            Icon(Icons.inbox_outlined, color: textDim, size: 36),
            SizedBox(height: 8),
            Text(
              '当前筛选分类下无文件',
              style: TextStyle(color: textDim, fontSize: 13),
            ),
          ],
        ),
      );
    }

    return Column(
      children: list.map((ReceivedFile file) {
        return _buildFileCard(context, file);
      }).toList(),
    );
  }

  Widget _buildFileCard(BuildContext context, ReceivedFile file) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: surfaceContainer,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: surfaceBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              // 文件图标
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: surfaceHigh,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: borderLuminescent),
                ),
                child: Icon(
                  _fileTypeIcon(file.name),
                  color: jade400,
                  size: 22,
                ),
              ),
              const SizedBox(width: 12),

              // 文件名与时间/来源
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: <Widget>[
                        Flexible(
                          child: Text(
                            file.name,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: textPrimary,
                              fontSize: 13,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        Text(
                          formatRelativeTime(file.receivedAt),
                          style: const TextStyle(
                            color: jade400,
                            fontSize: 10,
                            fontFamily: 'monospace',
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 3),
                    Row(
                      children: <Widget>[
                        Text(
                          formatBytes(file.size),
                          style: const TextStyle(
                            color: textPrimary,
                            fontSize: 10,
                            fontFamily: 'monospace',
                          ),
                        ),
                        const Text(' / ', style: TextStyle(color: textDim, fontSize: 10)),
                        Text(
                          file.sender.isNotEmpty ? file.sender : '局域网直连',
                          style: const TextStyle(color: textMuted, fontSize: 10),
                        ),
                      ],
                    ),
                    const SizedBox(height: 6),

                    // 安全校验标签
                    Row(
                      children: <Widget>[
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                          decoration: BoxDecoration(
                            color: surfaceLowest,
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: const Row(
                            children: <Widget>[
                              Icon(Icons.verified, color: badgeRoute, size: 10),
                              SizedBox(width: 3),
                              Text(
                                'BLAKE3 校验完成',
                                style: TextStyle(
                                  color: badgeRoute,
                                  fontSize: 9,
                                  fontFamily: 'monospace',
                                ),
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(width: 6),
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                          decoration: BoxDecoration(
                            color: jade400.withValues(alpha: 0.1),
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: const Text(
                            'ED25519-OK',
                            style: TextStyle(
                              color: jade300,
                              fontSize: 9,
                              fontFamily: 'monospace',
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),

          // 快捷动作抽屉栏
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            decoration: BoxDecoration(
              color: surfaceLowest.withValues(alpha: 0.5),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: <Widget>[
                InkWell(
                  onTap: () {
                    Clipboard.setData(ClipboardData(text: file.path));
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text('文件路径已复制'),
                        duration: Duration(seconds: 1),
                      ),
                    );
                  },
                  child: const Row(
                    children: <Widget>[
                      Icon(Icons.copy, size: 12, color: textMuted),
                      SizedBox(width: 3),
                      Text(
                        '复制路径',
                        style: TextStyle(color: textMuted, fontSize: 11),
                      ),
                    ],
                  ),
                ),
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 8),
                  child: Text('|', style: TextStyle(color: textDim, fontSize: 10)),
                ),
                InkWell(
                  onTap: () => widget.onOpenFile(file),
                  child: const Row(
                    children: <Widget>[
                      Icon(Icons.open_in_new, size: 12, color: jade400),
                      SizedBox(width: 3),
                      Text(
                        '打开',
                        style: TextStyle(
                          color: jade400,
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  IconData _fileTypeIcon(String name) {
    final String ext = name.toLowerCase();
    if (ext.endsWith('.pdf')) return Icons.picture_as_pdf;
    if (ext.endsWith('.zip') || ext.endsWith('.tar') || ext.endsWith('.gz') || ext.endsWith('.zst')) {
      return Icons.folder_zip;
    }
    if (ext.endsWith('.apk')) return Icons.android;
    if (ext.endsWith('.bin')) return Icons.memory;
    if (ext.endsWith('.png') || ext.endsWith('.jpg') || ext.endsWith('.jpeg')) {
      return Icons.image;
    }
    return Icons.insert_drive_file;
  }

  Widget _buildBatchControlBar(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: surfaceHigh,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: surfaceBorder),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: <Widget>[
          const Row(
            children: <Widget>[
              Icon(Icons.auto_awesome_motion, color: jade400, size: 16),
              SizedBox(width: 6),
              Text(
                '收件箱归档管理',
                style: TextStyle(color: textMuted, fontSize: 11),
              ),
            ],
          ),
          ElevatedButton.icon(
            onPressed: widget.onClearAll,
            icon: const Icon(Icons.delete_sweep, size: 14, color: dangerCoral),
            label: const Text(
              '清空记录',
              style: TextStyle(
                color: dangerCoral,
                fontSize: 11,
                fontWeight: FontWeight.w600,
              ),
            ),
            style: ElevatedButton.styleFrom(
              backgroundColor: dangerContainer.withValues(alpha: 0.6),
              elevation: 0,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              minimumSize: Size.zero,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(16),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
