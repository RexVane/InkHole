"""Wormhole 虫洞文件传输 — 入口模块。

python -m pyftp_server  等价于  python -m pyftp_server.wormhole.pet
"""
from pyftp_server.wormhole.pet import main

if __name__ == "__main__":
    main()
