"""
p2p.py
======
虫洞 P2P 引擎：局域网点对点文件传输，无需服务器。

架构：
  - mDNS (zeroconf) 自动发现局域网内其他虫洞节点
  - 直接 TCP 连接传输文件，无中转、无云端
  - 可选 AES-256-GCM 端到端加密(复用 crypto.py)

协议 (WHPP - Wormhole P2P Protocol)：
  [4B magic "WHPP"] [4B header_len] [header_len B JSON] [size B 文件数据]
  JSON header: {"filename": "...", "size": 12345, "encrypted": true/false}

使用：
  node = P2PNode(P2PConfig(inbox="~/wormhole"),
                 on_sent=..., on_received=..., on_status=..., on_peers_changed=...)
  node.start()           # 注册 mDNS + 启动 TCP 监听 + 开始发现
  node.select_peer(name) # 右键菜单选目标
  node.send_file(path)   # 发送给选中的对端
  node.stop()
"""

from __future__ import annotations
import os
import sys
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .crypto import encrypt, decrypt, is_encrypted

# ---------- 常量 ----------
_SERVICE_TYPE = "_wormhole._tcp.local."
_MAGIC = b"WHPP"          # Wormhole P2P Protocol magic
_BUFFER = 65536           # 64KB 传输块
_PORT_RANGE = (25000, 25100)   # 自动选端口范围


# ---------- 配置 ----------
@dataclass
class P2PConfig:
    inbox: str = "received"        # 收件箱：收到的文件落在这里
    listen_port: int = 0           # TCP 监听端口；0 = 从 _PORT_RANGE 自动选
    peer_name: str = ""            # 本机显示名；空则用 hostname
    secret: str = ""               # 端到端加密口令(两台电脑必须一致；空=不加密)

    def __post_init__(self):
        if not self.peer_name:
            self.peer_name = socket.gethostname()


class PeerInfo:
    """一个已发现的对端节点。"""
    __slots__ = ("name", "host", "port")

    def __init__(self, name: str, host: str, port: int):
        self.name = name
        self.host = host
        self.port = port

    def __repr__(self):
        return f"PeerInfo({self.name!r}, {self.host}:{self.port})"

    def __str__(self):
        return f"{self.name} ({self.host})"


# ---------- P2P 引擎 ----------
class P2PNode:
    """局域网点对点文件传输节点。

    职责（纯后台，可自动化测试，不依赖任何 GUI）：
      start()/stop()     注册 mDNS 服务 + 启动 TCP 监听 + 发现其他节点
      send_file(path)    把文件直接发给选中的对端(TCP 直连)
      peers()            返回已发现的对端列表
      select_peer(name)  选择发送目标
      回调钩子           on_sent / on_received / on_status / on_peers_changed
    """

    def __init__(self, cfg: P2PConfig,
                 on_sent: Callable[[str], None] | None = None,
                 on_received: Callable[[str], None] | None = None,
                 on_status: Callable[[str], None] | None = None,
                 on_peers_changed: Callable[[], None] | None = None):
        self.cfg = cfg
        self.on_sent = on_sent
        self.on_received = on_received
        self.on_status = on_status
        self.on_peers_changed = on_peers_changed

        self._peers: dict[str, PeerInfo] = {}   # name -> PeerInfo
        self._lock = threading.Lock()
        self._selected_peer: str | None = None  # 当前选中的目标
        self._running = False

        # mDNS 相关
        self._zc = None              # Zeroconf 实例
        self._browser = None         # ServiceBrowser
        self._listener = None        # _WormholeListener
        self._service_info = None    # 自己注册的 ServiceInfo
        self._actual_port = 0        # 实际监听端口

        # TCP 服务器
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

        if cfg.secret:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            except ImportError:
                raise SystemExit("端到端加密(--secret)需要 cryptography 库：pip install cryptography")

        os.makedirs(self.cfg.inbox, exist_ok=True)

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """启动：注册 mDNS + 启动 TCP 监听 + 开始发现其他节点。"""
        if self._running:
            return
        self._running = True

        # 1. 启动 TCP 监听
        self._start_tcp_server()

        # 2. 注册 mDNS 服务
        try:
            from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser
        except ImportError:
            raise SystemExit("P2P 模式需要 zeroconf 库：pip install zeroconf")

        local_ip = self._get_local_ip()
        self._zc = Zeroconf()
        self._service_info = ServiceInfo(
            type_=_SERVICE_TYPE,
            name=f"{self.cfg.peer_name}.{_SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=self._actual_port,
            properties={b"peer_name": self.cfg.peer_name.encode("utf-8")},
        )
        self._zc.register_service(self._service_info)

        # 3. 开始发现其他节点
        self._listener = _WormholeListener(self)
        self._browser = ServiceBrowser(self._zc, _SERVICE_TYPE, self._listener)

        self._status(f"虫洞已开启 · {self.cfg.peer_name} @ {local_ip}:{self._actual_port}")

    def stop(self) -> None:
        """停止：注销 mDNS + 关闭 TCP 监听。"""
        self._running = False
        if self._browser:
            try:
                self._browser.cancel()
            except Exception:
                pass
            self._browser = None
        if self._zc:
            try:
                if self._service_info:
                    self._zc.unregister_service(self._service_info)
            except Exception:
                pass
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None

    # ---------- TCP 服务器 ----------
    def _start_tcp_server(self) -> None:
        """绑定 TCP 监听端口。listen_port=0 时由操作系统自动分配可用端口。"""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        port = self.cfg.listen_port
        # 统一交给 OS 分配(port=0)或绑定指定端口。不用手动扫端口范围——
        # Windows 上 SO_REUSEADDR 允许多 socket 绑同一端口,手动扫描会给两个节点
        # 分到同一个端口,导致只有一方能收到连接。
        self._server_sock.bind(("", port))

        self._server_sock.listen(8)
        self._actual_port = self._server_sock.getsockname()[1]

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        """接受 TCP 连接，每条连一个线程处理接收。"""
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                break  # server socket closed
            threading.Thread(target=self._handle_connection, args=(conn, addr), daemon=True).start()

    def _handle_connection(self, conn: socket.socket, addr) -> None:
        """接收一个文件：读 WHPP 头 + 文件数据，落盘到收件箱。"""
        part_path = None
        try:
            # 读 magic
            magic = _recv_exact(conn, 4)
            if magic != _MAGIC:
                return

            # 读 header 长度 + header JSON
            hdr_len_bytes = _recv_exact(conn, 4)
            if not hdr_len_bytes:
                return
            hdr_len = struct.unpack("!I", hdr_len_bytes)[0]
            hdr_bytes = _recv_exact(conn, hdr_len)
            if not hdr_bytes:
                return
            header = json.loads(hdr_bytes.decode("utf-8"))

            filename = os.path.basename(header.get("filename", "unknown"))  # 防 ../ 路径穿越
            size = header.get("size", 0)
            encrypted = header.get("encrypted", False)

            # 流式写入 .part，完成后原子改名
            dst = os.path.join(self.cfg.inbox, filename)
            part_path = dst + ".part"
            remaining = size
            with open(part_path, "wb") as f:
                while remaining > 0:
                    chunk = conn.recv(min(_BUFFER, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            # 端到端加密文件：落盘前原地解密
            if encrypted and self.cfg.secret:
                with open(part_path, "rb") as f:
                    blob = f.read()
                plain = decrypt(self.cfg.secret, blob)
                if plain is not None:
                    with open(part_path, "wb") as f:
                        f.write(plain)
                else:
                    self._status("解密失败")
            elif encrypted and not self.cfg.secret:
                self._status("收到加密文件，但未设口令")

            os.replace(part_path, dst)
            part_path = None

            sender = f"{addr[0]}:{addr[1]}"
            if self.on_received:
                self.on_received(dst)
        except Exception as e:
            self._status("接收失败", str(e))
        finally:
            conn.close()
            if part_path and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    # ---------- 发送文件 ----------
    def send_file(self, local_path: str) -> bool:
        """把文件直接发给选中的对端。成功返回 True。"""
        if not os.path.isfile(local_path):
            return False

        with self._lock:
            peer = self._peers.get(self._selected_peer) if self._selected_peer else None
        if not peer:
            self._status("请先右键选择目标设备")
            return False

        name = os.path.basename(local_path)
        try:
            # 准备文件数据：加密则整块读入内存加密，不加密则流式发送
            if self.cfg.secret:
                with open(local_path, "rb") as f:
                    data = encrypt(self.cfg.secret, f.read())
                encrypted = True
            else:
                data = None
                encrypted = False

            file_size = len(data) if data is not None else os.path.getsize(local_path)
            header = json.dumps({
                "filename": name,
                "size": file_size,
                "encrypted": encrypted,
            }).encode("utf-8")

            sock = socket.create_connection((peer.host, peer.port), timeout=15)
            try:
                sock.sendall(_MAGIC)
                sock.sendall(struct.pack("!I", len(header)))
                sock.sendall(header)

                if data is not None:
                    # 加密数据已在内存，分块发送
                    offset = 0
                    while offset < len(data):
                        sock.sendall(data[offset:offset + _BUFFER])
                        offset += _BUFFER
                else:
                    # 明文：流式从磁盘读
                    with open(local_path, "rb") as f:
                        while True:
                            chunk = f.read(_BUFFER)
                            if not chunk:
                                break
                            sock.sendall(chunk)
            finally:
                sock.close()
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self._status(f"无法连接 {peer.name}", str(e))
            return False
        except Exception as e:
            self._status("发送失败", str(e))
            return False

        if self.on_sent:
            self.on_sent(name)
        return True

    # ---------- 对端管理 ----------
    def peers(self) -> list[PeerInfo]:
        """返回已发现的对端列表。"""
        with self._lock:
            return list(self._peers.values())

    def peer_names(self) -> list[str]:
        """返回已发现的对端名称列表。"""
        with self._lock:
            return list(self._peers.keys())

    def selected_peer(self) -> str | None:
        """当前选中的目标对端名；None = 未选。"""
        with self._lock:
            return self._selected_peer

    def select_peer(self, name: str | None) -> None:
        """选择发送目标。传 None 取消选择。"""
        with self._lock:
            if name is None or name in self._peers:
                self._selected_peer = name
        label = name if name else "未选择"
        self._status(f"目标: {label}")

    def _on_peer_added(self, name: str, host: str, port: int) -> None:
        """mDNS 发现新节点时调用（由 _WormholeListener 触发）。"""
        with self._lock:
            self._peers[name] = PeerInfo(name, host, port)

        self._status(f"发现 {name}")

        if self.on_peers_changed:
            self.on_peers_changed()

    def _on_peer_removed(self, name: str) -> None:
        """mDNS 节点离线时调用。"""
        with self._lock:
            if name in self._peers:
                del self._peers[name]
            if self._selected_peer == name:
                self._selected_peer = None    # 选中的离线了，不自动切换，由用户重新选

        self._status(f"{name} 离线")

        if self.on_peers_changed:
            self.on_peers_changed()

    # ---------- 工具 ----------
    def _status(self, msg: str, detail: str = "") -> None:
        if detail:
            print(f"[P2P] {msg} | {detail}", flush=True)
        if self.on_status:
            self.on_status(msg)

    # ---------- 工具 ----------
    @staticmethod
    def _get_local_ip() -> str:
        """获取本机局域网 IP（通过 UDP connect 探测，不实际发包）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    @property
    def actual_port(self) -> int:
        return self._actual_port


# ---------- mDNS 监听器 ----------
class _WormholeListener:
    """zeroconf ServiceBrowser 回调：发现/离线时更新 P2PNode 的对端表。"""

    def __init__(self, node: P2PNode):
        self._node = node

    def add_service(self, zc, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=2000)
        if info is None:
            return
        # 优先从 properties 读 peer_name，回退到服务名解析
        props = {}
        if info.properties:
            for k, v in info.properties.items():
                key = k.decode("utf-8") if isinstance(k, bytes) else k
                val = v.decode("utf-8") if isinstance(v, bytes) else v
                props[key] = val
        peer_name = props.get("peer_name", "")
        if not peer_name:
            # 回退：从服务名 "MyPC._wormhole._tcp.local." 提取 "MyPC"
            peer_name = name.split(".")[0] if name else "unknown"

        # 不添加自己
        if peer_name == self._node.cfg.peer_name:
            return

        addresses = info.parsed_addresses()
        if not addresses:
            return

        self._node._on_peer_added(peer_name, addresses[0], info.port)

    def remove_service(self, zc, type_: str, name: str) -> None:
        # 从 properties 拿不到离线节点信息，回退到服务名解析
        peer_name = name.split(".")[0] if name else "unknown"
        # 但我们自己登记时用了 properties，这里也试试更精确的解析
        suffix = "." + _SERVICE_TYPE
        if name.endswith(suffix):
            peer_name = name[:-len(suffix)]
        self._node._on_peer_removed(peer_name)

    def update_service(self, zc, type_: str, name: str) -> None:
        pass  # 不需要处理


# ---------- 网络工具 ----------
def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """从 socket 精确读取 n 字节。连接中断返回 None。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


# ---------- 命令行入口 ----------
def _run_cli(argv=None) -> None:
    """无 GUI 模式：监视发件箱自动发送 + 收件箱自动接收。"""
    import argparse
    ap = argparse.ArgumentParser(description="虫洞 P2P(命令行版，无动画)")
    ap.add_argument("--inbox", default="received", help="收件箱目录")
    ap.add_argument("--outbox", default="", help="监视目录：放入文件即自动发送")
    ap.add_argument("--port", type=int, default=0, help="监听端口(0=自动)")
    ap.add_argument("--name", default="", help="本机显示名(默认 hostname)")
    ap.add_argument("--secret", default="", help="端到端加密口令")
    args = ap.parse_args(argv)

    cfg = P2PConfig(inbox=args.inbox, listen_port=args.port,
                    peer_name=args.name, secret=args.secret)
    node = P2PNode(
        cfg,
        on_sent=lambda n: print(f"[发送] {n} 已吸入虫洞"),
        on_received=lambda p: print(f"[接收] 虫洞吐出 -> {p}"),
        on_status=lambda s: print(f"[状态] {s}"),
        on_peers_changed=lambda: print(f"[设备] {', '.join(node.peer_names()) or '无'}"),
    )
    node.start()
    print(f"虫洞 P2P 已启动 · 收件箱={os.path.abspath(args.inbox)}")

    outbox = args.outbox
    sent_outbox: set[str] = set()
    if outbox:
        os.makedirs(outbox, exist_ok=True)
        print(f"监视发送目录={os.path.abspath(outbox)}")
    try:
        while True:
            if outbox and node.selected_peer():
                for fn in os.listdir(outbox):
                    fp = os.path.join(outbox, fn)
                    if os.path.isfile(fp) and fp not in sent_outbox and not fn.startswith("."):
                        if node.send_file(fp):
                            sent_outbox.add(fp)
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()
        print("\n已停止")


if __name__ == "__main__":
    _run_cli()
