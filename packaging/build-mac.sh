#!/usr/bin/env bash
# macOS 上构建墨洞桌宠.app(标准 onedir .app 包)。
# 用法:bash packaging/build-mac.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 检查依赖"
python3 -c "import PySide6" 2>/dev/null   || pip3 install PySide6
python3 -c "import zeroconf" 2>/dev/null  || pip3 install zeroconf
python3 -c "import cryptography" 2>/dev/null || pip3 install cryptography
python3 -c "import psutil" 2>/dev/null || pip3 install psutil
python3 -c "import PyInstaller" 2>/dev/null  || pip3 install pyinstaller
# 可选:常驻所有桌面(Spaces)效果需要 pyobjc;不装也能正常用
python3 -c "import AppKit" 2>/dev/null || pip3 install pyobjc-framework-Cocoa || true

echo "==> 打包"
pyinstaller inkhole-pet.spec --noconfirm --clean

echo "==> 完成:dist/InkHolePet.app(Finder 显示名仍是墨洞桌宠)"
echo "   运行示例:"
echo "   ./dist/InkHolePet.app/Contents/MacOS/InkHolePet --name 我的Mac --secret '口令'"
