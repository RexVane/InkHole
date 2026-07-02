"""
轻量 app 打包入口。

PyInstaller 需要一个顶层脚本作入口;桌宠模块 pet.py 内部用相对导入
(from .p2p import ...),不能直接当 __main__ 跑,故用本文件以"包"的方式
调用 pet.main()。源码运行(python packaging/pet_entry.py)与打包后均适用。
"""
import os
import sys

# 源码直跑时把 src 加进路径;打包后 pyftp_server 已随包冻结,这步无害。
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, _SRC)

from pyftp_server.wormhole.pet import main

if __name__ == "__main__":
    main()
