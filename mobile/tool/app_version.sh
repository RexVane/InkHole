#!/usr/bin/env bash
# 从 pubspec.yaml 读取应用版本，供构建入口注入 --dart-define=APP_VERSION。
#
# 版本号在本仓库有三处体现，pubspec.yaml 是唯一权威来源：
#   1. rust/Cargo.toml        [workspace.package] version  —— 核心库版本
#   2. mobile/pubspec.yaml    version                      —— 权威来源（Flutter 用它生成
#                                                            Android versionName / iOS
#                                                            CFBundleShortVersionString）
#   3. mobile/lib/models.dart appVersion 的默认值           —— 仅作 flutter run 开发兜底
#
# 三者的语义版本部分必须一致，由 `cargo test --workspace` 里的
# `version_sources_agree` 测试强制校验（CI 也会跑到）。
#
# 用法:
#   app_version.sh            -> 2.0.14+15  完整版本（含构建号）
#   app_version.sh --semver   -> 2.0.14     语义版本（用于 APP_VERSION 与更新比对）
set -euo pipefail

pubspec="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pubspec.yaml"
raw="$(sed -n 's/^version:[[:space:]]*//p' "$pubspec" | head -n 1 | tr -d '[:space:]')"
if [ -z "$raw" ]; then
  echo "pubspec.yaml has no version field" >&2
  exit 1
fi

case "${1:-}" in
  --semver) printf '%s\n' "${raw%%+*}" ;;
  "") printf '%s\n' "$raw" ;;
  *)
    echo "unknown argument: $1" >&2
    exit 2
    ;;
esac
