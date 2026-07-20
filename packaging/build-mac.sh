#!/usr/bin/env bash
# macOS 上构建墨洞桌宠.app(标准 onedir .app 包)。
# 用法:bash packaging/build-mac.sh
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_ROOT="$(cd .. && pwd)"

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi
PIP_BIN=("$PYTHON_BIN" -m pip)
PYINSTALLER_BIN=("$PYTHON_BIN" -m PyInstaller)

echo "==> 检查依赖"
"$PYTHON_BIN" -c "import PySide6" 2>/dev/null   || "${PIP_BIN[@]}" install PySide6
"$PYTHON_BIN" -c "import zeroconf" 2>/dev/null  || "${PIP_BIN[@]}" install zeroconf
"$PYTHON_BIN" -c "import cryptography" 2>/dev/null || "${PIP_BIN[@]}" install cryptography
"$PYTHON_BIN" -c "import psutil" 2>/dev/null || "${PIP_BIN[@]}" install psutil
"$PYTHON_BIN" -c "import keyring" 2>/dev/null || "${PIP_BIN[@]}" install keyring
"$PYTHON_BIN" -c "import qrcode" 2>/dev/null || "${PIP_BIN[@]}" install 'qrcode[pil]'
"$PYTHON_BIN" -c "import PyInstaller" 2>/dev/null  || "${PIP_BIN[@]}" install pyinstaller
# 原生文件/文件夹混选与常驻所有桌面(Spaces)使用 pyobjc;安装失败时有 Qt 后备
"$PYTHON_BIN" -c "import AppKit" 2>/dev/null || "${PIP_BIN[@]}" install pyobjc-framework-Cocoa || true

echo "==> 编译共享跨网核心"
mkdir -p "$PROJECT_ROOT/transport-core/bin"
(cd "$PROJECT_ROOT/transport-core" && \
  go build -trimpath -ldflags="-s -w" -o bin/inkhole-core ./cmd/inkhole-core)

echo "==> 打包"
"${PYINSTALLER_BIN[@]}" inkhole-pet.spec --noconfirm --clean

echo "==> 完成:dist/InkHolePet.app(Finder 显示名仍是墨洞桌宠)"
echo "   运行示例:"
echo "   ./dist/InkHolePet.app/Contents/MacOS/InkHolePet --name 我的Mac --secret '口令'"
