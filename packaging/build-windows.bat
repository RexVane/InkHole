@echo off
REM Windows 上构建墨洞桌面客户端(onedir 目录)。
REM 用法:双击本文件,或在 packaging 目录执行 build-windows.bat
cd /d "%~dp0"

echo ==^> 检查依赖
python -c "import PySide6" 2>nul || pip install PySide6
python -c "import zeroconf" 2>nul || pip install zeroconf
python -c "import cryptography" 2>nul || pip install cryptography
python -c "import psutil" 2>nul || pip install psutil
python -c "import PyInstaller" 2>nul || pip install pyinstaller

echo ==^> 打包
pyinstaller inkhole-pet.spec --noconfirm --clean

echo ==^> 完成:dist\InkHolePet\InkHolePet.exe(界面显示名仍是"墨洞")
echo    运行示例:
echo    dist\InkHolePet\InkHolePet.exe --name 我的电脑 --secret "口令"
pause
