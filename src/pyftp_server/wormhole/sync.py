"""
sync.py
=======
虫洞同步引擎：把 FTP 服务器上的一个共享频道(/wormhole)当中转站，
两台电脑各跑一个 WormholeSync，实现"一台拖入 → 另一台自动出现"。

职责（纯后台，可自动化测试，不依赖任何 GUI）：
  send_file(path)   把一个文件上传到频道(先传 name.part 再 RNTO 改名，避免半截文件被对端看到)
  start()/stop()    后台轮询线程：每隔 interval 秒列出频道文件，
                    凡是"已收清单"里没有、且不是自己刚发的，就下载到收件箱
  去重              .wormhole_seen.json 记录已处理过的文件指纹，避免重复下载/死循环
  断线重连          连接异常时自动重连，不中断同步
  回调钩子          on_sent / on_received / on_status 供桌宠挂件播放吸入/喷出动画

设计要点 —— 如何避免"死循环"(B 收到的文件又被 B 传回去)：
  每个文件以 "文件名|大小" 作为指纹存入 seen 集合。无论是自己发出的、还是
  从频道下载到收件箱的，都登记进 seen；轮询时只处理 seen 里没有的新指纹。
  因此下载下来的文件不会被再次上传，发出去的文件回头看到也不会重复下载。
"""

from __future__ import annotations
import io
import os
import ssl
import json
import time
import socket
import threading
from dataclasses import dataclass, field
from ftplib import FTP, FTP_TLS, error_perm, all_errors
from typing import Callable


# ---------- 配置 ----------
@dataclass
class WormholeConfig:
    host: str = "127.0.0.1"        # FTP 服务器地址(局域网填服务器内网 IP)
    port: int = 2121
    user: str = "wormhole"         # 共享频道账号
    password: str = "wormhole"
    inbox: str = "received"        # 收件箱：收到的文件落在这里(可改成桌面等任意目录)
    interval: float = 2.0          # 轮询间隔(秒)
    burn: bool = True              # 阅后即焚：下载成功后删除服务器中转副本(>2台设备时请关闭)
    tls: bool = False              # FTPS：TLS 加密控制与数据连接(服务器需 --tls 启动)
    secret: str = ""               # 端到端加密口令(两台电脑必须一致；空=不加密)
    state_file: str = ""           # 已收清单文件路径；空则放在 inbox/.wormhole_seen.json
    peer_id: str = ""              # 本机标识(用于日志区分两台电脑)；空则用主机名

    def __post_init__(self):
        if not self.peer_id:
            self.peer_id = socket.gethostname()
        if not self.state_file:
            self.state_file = os.path.join(self.inbox, ".wormhole_seen.json")


# ---------- 端到端加密(AES-256-GCM) ----------
# 加密实现已抽取到 crypto.py，被 sync.py(FTP 中转) 和 p2p.py(局域网直连) 共用。
from .crypto import encrypt as _encrypt, decrypt as _decrypt, is_encrypted, _MAGIC


# ---------- 同步引擎 ----------
class WormholeSync:
    def __init__(self, cfg: WormholeConfig,
                 on_sent: Callable[[str], None] | None = None,
                 on_received: Callable[[str], None] | None = None,
                 on_status: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.on_sent = on_sent            # 上传完成回调(参数=文件名) -> 播放吸入动画
        self.on_received = on_received    # 下载完成回调(参数=收件箱内文件路径) -> 播放喷出动画
        self.on_status = on_status        # 状态变化回调(参数=文字) -> 显示"已连接/重连中"
        self._ftp: FTP | None = None
        self._seen: set[str] = set()      # 已处理文件指纹集合
        self._lock = threading.Lock()     # 串行化对 FTP 连接的访问
        self._running = False
        self._paused = False              # 暂停同步(托盘菜单可切换)
        self._connected = False           # 当前是否连上服务器(由轮询结果驱动,零额外流量)
        self._thread: threading.Thread | None = None
        if cfg.secret:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            except ImportError:
                raise SystemExit("端到端加密(--secret)需要 cryptography 库：pip install cryptography")
        os.makedirs(self.cfg.inbox, exist_ok=True)
        self._load_seen()

    # ---------- 已收清单(去重) ----------
    def _load_seen(self) -> None:
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                self._seen = set(json.load(f))
        except (OSError, ValueError):
            self._seen = set()

    def _save_seen(self) -> None:
        try:
            with open(self.cfg.state_file, "w", encoding="utf-8") as f:
                json.dump(sorted(self._seen), f, ensure_ascii=False, indent=0)
        except OSError:
            pass

    @staticmethod
    def _fingerprint(name: str, size: int) -> str:
        """文件指纹：名字 + 大小。简单可靠，足以区分频道里的不同文件版本。"""
        return f"{name}|{size}"

    def _status(self, msg: str, detail: str = "") -> None:
        # detail 不为空时,把含具体错误的完整信息写进日志(stdout->日志文件),
        # 但桌宠界面只显示简短的 msg,保持清爽又不丢排查信息。
        if detail:
            print(f"[状态] {msg} | 详情: {detail}", flush=True)
        if self.on_status:
            self.on_status(msg)

    # ---------- 连接管理(断线重连) ----------
    def _connect(self) -> FTP:
        if self.cfg.tls:
            # 自签证书场景：不校验证书链(防窃听已足够；防中间人需配 CA 校验，见文档)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ftp: FTP = FTP_TLS(context=ctx)
        else:
            ftp = FTP()
        ftp.connect(self.cfg.host, self.cfg.port, timeout=15)
        ftp.login(self.cfg.user, self.cfg.password)   # FTP_TLS.login 自动先发 AUTH TLS
        if isinstance(ftp, FTP_TLS):
            ftp.prot_p()                              # 数据连接也加密(PROT P)
        ftp.set_pasv(True)
        return ftp

    def _ensure_ftp(self) -> FTP:
        """返回可用连接；断了就重连。"""
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")     # 探活
                return self._ftp
            except all_errors:
                try:
                    self._ftp.close()
                except Exception:
                    pass
                self._ftp = None
        self._status("连接中…")
        self._ftp = self._connect()
        self._status("已连接")
        return self._ftp

    # ---------- 上传：发送文件到频道 ----------
    def send_file(self, local_path: str) -> bool:
        """把本地文件上传到频道。先传为 name.part，传完 RNTO 改成正名，
        避免对端在传输途中看到半截文件。成功后登记指纹并触发 on_sent。
        设置了 secret 时上传前先端到端加密(服务器只见密文)。"""
        if not os.path.isfile(local_path):
            return False
        name = os.path.basename(local_path)
        with self._lock:
            try:
                ftp = self._ensure_ftp()
                tmp = name + ".part"
                if self.cfg.secret:
                    with open(local_path, "rb") as f:
                        blob = _encrypt(self.cfg.secret, f.read())
                    size = len(blob)              # 指纹必须用密文大小(与频道列表一致)
                    ftp.storbinary(f"STOR {tmp}", io.BytesIO(blob))
                else:
                    size = os.path.getsize(local_path)
                    with open(local_path, "rb") as f:
                        ftp.storbinary(f"STOR {tmp}", f)
                # 改成正名（若同名已存在，先删旧的）
                try:
                    ftp.delete(name)
                except error_perm:
                    pass
                ftp.rename(tmp, name)
                # 登记指纹：自己发出的文件，回头轮询时不会再下载回来
                self._seen.add(self._fingerprint(name, size))
                self._save_seen()
            except all_errors as e:
                self._status("上传失败，将重连", detail=str(e))
                self._ftp = None
                return False
        if self.on_sent:
            self.on_sent(name)
        return True

    # ---------- 轮询 + 下载 ----------
    def _maybe_decrypt(self, path: str) -> None:
        """下载落盘前处理端到端加密：是加密格式且口令正确则原地解密为明文；
        没设口令或口令不对则按密文保存(不丢数据)并提示。"""
        with open(path, "rb") as f:
            head = f.read(4)
            if head != _MAGIC:
                return                          # 普通明文文件，原样保存
            blob = head + f.read()
        if not self.cfg.secret:
            self._status("收到加密文件，但未设口令")
            return
        plain = _decrypt(self.cfg.secret, blob)
        if plain is None:
            self._status("解密失败")
            return
        with open(path, "wb") as f:
            f.write(plain)

    def _list_channel(self, ftp: FTP) -> list[tuple[str, int]]:
        """列出频道里的文件名与大小，忽略 .part 临时文件与隐藏文件。"""
        items: list[tuple[str, int]] = []
        names = ftp.nlst()
        for n in names:
            if n in (".", "..") or n.endswith(".part") or n.startswith("."):
                continue
            try:
                size = ftp.size(n) or 0
            except all_errors:
                size = 0
            items.append((n, size))
        return items

    def poll_once(self) -> list[str]:
        """轮询一次：把频道里"没收过"的文件下载到收件箱。返回本次新收文件名列表。"""
        received: list[str] = []
        with self._lock:
            try:
                ftp = self._ensure_ftp()
                for name, size in self._list_channel(ftp):
                    fp = self._fingerprint(name, size)
                    if fp in self._seen:
                        continue                      # 已收过/自己发的，跳过(防死循环)
                    # 原子化下载：先写 .part，完成后改名
                    dst = os.path.join(self.cfg.inbox, name)
                    part = dst + ".part"
                    try:
                        with open(part, "wb") as f:
                            ftp.retrbinary(f"RETR {name}", f.write)
                        self._maybe_decrypt(part)     # 端到端加密文件：落盘前原地解密
                        os.replace(part, dst)
                    except all_errors as e:   # all_errors 已含 OSError/EOFError，不能再套元组
                        self._status(f"{name} 下载失败", detail=str(e))
                        try:
                            os.remove(part)
                        except OSError:
                            pass
                        continue
                    self._seen.add(fp)
                    received.append(dst)
                    if self.cfg.burn:
                        try:
                            ftp.delete(name)   # 阅后即焚：中转站不留存文件
                        except all_errors:
                            pass               # 删失败不影响接收；指纹已记，不会重复下载
                if received:
                    self._save_seen()
                self._set_connected(True)         # 轮询顺利跑完 = 当前已连接(免费,搭轮询便车)
            except all_errors as e:
                self._set_connected(False)        # 轮询异常 = 当前未连接
                self._status("轮询失败，将重连", detail=str(e))
                self._ftp = None
        # 回调在锁外触发，避免动画回调里再调 send 造成自锁
        for dst in received:
            if self.on_received:
                self.on_received(dst)
        return received

    # ---------- 后台线程 ----------
    def _loop(self) -> None:
        while self._running:
            try:
                if not self._paused:          # 暂停时只休眠,不收发(省流量、不打扰)
                    self.poll_once()
            except Exception as e:    # 兜底：后台线程绝不能死，任何意外只记状态继续轮询
                self._status("轮询异常", detail=str(e))
                self._ftp = None
            time.sleep(self.cfg.interval)

    def set_paused(self, paused: bool) -> None:
        """暂停/恢复同步。暂停时停止轮询收发,但保持线程存活;恢复后立即继续。"""
        self._paused = paused
        self._status("已暂停同步" if paused else "已恢复同步")

    def is_paused(self) -> bool:
        return self._paused

    def is_connected(self) -> bool:
        return self._connected

    def _set_connected(self, ok: bool) -> None:
        """记录连接状态;仅在状态翻转时推一次提示,避免每 2 秒刷屏。"""
        if ok != self._connected:
            self._connected = ok
            self._status("已连接" if ok else "连接断开，重连中")

    def start(self) -> None:
        """启动后台轮询线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._ftp is not None:
                try:
                    self._ftp.quit()
                except all_errors:
                    pass
                self._ftp = None


# ---------- 命令行入口：无 GUI 也能跑同步(放进监视文件夹即自动发送) ----------
def _run_cli(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="虫洞同步引擎(命令行版，无动画)")
    ap.add_argument("--host", default="127.0.0.1", help="FTP 服务器地址")
    ap.add_argument("--port", type=int, default=2121)
    ap.add_argument("--user", default="wormhole")
    ap.add_argument("--password", default="wormhole")
    ap.add_argument("--inbox", default="received", help="收件箱目录(收到的文件放这)")
    ap.add_argument("--outbox", default="", help="监视目录：放进来的文件自动发送")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--no-burn", action="store_true",
                    help="关闭阅后即焚(>2台设备时使用，保留中转副本供其他设备下载)")
    ap.add_argument("--tls", action="store_true", help="FTPS 加密连接(服务器需 --tls 启动)")
    ap.add_argument("--secret", default="", help="端到端加密口令(两台电脑必须一致；需 cryptography 库)")
    args = ap.parse_args(argv)

    cfg = WormholeConfig(host=args.host, port=args.port, user=args.user,
                         password=args.password, inbox=args.inbox, interval=args.interval,
                         burn=not args.no_burn, tls=args.tls, secret=args.secret)
    sync = WormholeSync(
        cfg,
        on_sent=lambda n: print(f"[发送] {n} 已吸入虫洞"),
        on_received=lambda p: print(f"[接收] 虫洞吐出 -> {p}"),
        on_status=lambda s: print(f"[状态] {s}"),
    )
    sync.start()
    print(f"虫洞同步中… 收件箱={os.path.abspath(args.inbox)}")
    outbox = args.outbox
    sent_outbox: set[str] = set()
    if outbox:
        os.makedirs(outbox, exist_ok=True)
        print(f"监视发送目录={os.path.abspath(outbox)} (放入文件即自动发送)")
    try:
        while True:
            if outbox:
                for fn in os.listdir(outbox):
                    fp = os.path.join(outbox, fn)
                    if os.path.isfile(fp) and fp not in sent_outbox and not fn.startswith("."):
                        if sync.send_file(fp):
                            sent_outbox.add(fp)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sync.stop()
        print("\n已停止")


if __name__ == "__main__":
    _run_cli()
