@echo off
REM Windows 上构建墨洞桌宠.exe(单文件 onefile)。
REM 用法:双击本文件,或在 packaging 目录执行 build-windows.bat
cd /d "%~dp0"

echo ==^> 检查依赖
python -c "import PySide6" 2>nul || pip install PySide6
python -c "import zeroconf" 2>nul || pip install zeroconf
python -c "import cryptography" 2>nul || pip install cryptography
python -c "import PyInstaller" 2>nul || pip install pyinstaller

echo ==^> 打包
pyinstaller wormhole-pet.spec --noconfirm --clean

echo ==^> 完成:dist\墨洞桌宠.exe
echo    运行示例:
echo    dist\墨洞桌宠.exe --name 我的电脑 --secret "口令"
pause
