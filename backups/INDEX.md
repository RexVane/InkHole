2026-10-19 15:05:52 | mobile/lib/widgets/settings_dialog.dart | bb1ef6a | 清理旧版设置弹窗死代码（已迁移至 Tab 4 SettingsView）
2026-10-19 15:05:52 | mobile/lib/widgets/usage_guide_dialog.dart | bb1ef6a | 清理仅被废弃 settings_dialog 引用的旧版使用说明弹窗死代码
2026-10-19 15:05:52 | mobile/lib/widgets/device_chip.dart | bb1ef6a | 清理旧版设备胶囊死代码（已由 device_orbit_node 替代）
2026-10-19 15:05:52 | mobile/lib/widgets/file_card.dart | bb1ef6a | 清理未被引用的旧版接收记录卡片死代码
2026-10-19 15:05:52 | mobile/lib/widgets/ink_hole.dart | bb1ef6a | 清理旧版墨洞英雄区死代码（已由 cyber_radar 替代）
2026-10-19 15:05:52 | mobile/lib/widgets/wormhole_dialog.dart | bb1ef6a | 清理与 Tab 3 PairView 重复的旧版短码弹窗
2026-10-20 00:19 | branch feat/stage2-starfield-window | 29daa6e | 清理上游已删除([gone])的分支：已完全合并进 main，无需实体备份
2026-10-20 00:19 | branch chore/ci-run-all-tests | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:19 | branch feat/stage2-binary-star | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:19 | branch feat/stage2-binary-star-qml | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:19 | branch feat/stage2-folder-send | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:19 | branch feat/stage2-history | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:19 | branch feat/stage2-settings | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:19 | branch feat/stage2-starfield-qml | backups/git/gone-branches-20261020-001856.bundle | 清理上游已删除([gone])的分支：未合并进 main，先备份
2026-10-20 00:20 | backups/git/gone-branches-20261020-001856.bundle | 回收站 | 用户要求不做额外备份，只保留 GitHub 版本，撤销本次 bundle 备份
2026-10-20 00:20 | backups/git/gone-branches-20261020-001856.README.txt | 回收站 | 同上，随 bundle 一并撤销
2026-10-20 00:20 | tag backup/before-rollback-20260617 | 762a29c | 用户要求只保留 GitHub 版本，删除本地独有标签；如需恢复: git tag backup/before-rollback-20260617 762a29c
2026-10-20 00:20 | branch wip/ssh-relay-android | 1a18468 | 旧架构遗留分支(android/desktop/python 路径在 main 中已不存在)，用户要求只保留 GitHub 版本；如需找回: git branch wip/ssh-relay-android 1a18468
2026-10-20 00:20 | stash codex-pre-pull-2026-07-29 | 51ab020 | 补丁作用于已移除的 desktop/ 目录，无法再应用，用户要求只保留 GitHub 版本；如需找回: git stash store -m <说明> 51ab020
