"""
p2p.py
======
墨洞 P2P 引擎：局域网点对点文件传输，无需服务器。

架构：
  - mDNS (zeroconf) 自动发现局域网内其他墨洞节点
  - 直接 TCP 连接传输文件，无中转、无云端
  - 可选 AES-256-GCM 端到端加密(复用 crypto.py)

协议 (WHPP - InkHole P2P Protocol)：
  [4B magic "WHPP"] [4B header_len] [header_len B JSON] [size B 文件数据]
  JSON header: {"filename": "...", "size": 12345, "encrypted": true/false}

使用：
  node = P2PNode(P2PConfig(inbox="~/inkhole"),
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
import shutil
import socket
import struct
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .crypto import (encrypt, decrypt, is_encrypted, encrypt_chunks,
                     chunked_wire_size, ChunkedDecryptor, CHUNK_SIZE)

# ---------- 常量 ----------
_SERVICE_TYPE = "_inkhole._tcp.local."
_MAGIC = b"WHPP"          # InkHole P2P Protocol magic
_BUFFER = 65536           # 64KB 传输块
_MAX_HEADER = 64 * 1024            # header JSON 长度上限(来自网络，不可信)
_MAX_FILE_SIZE = 1 << 40           # 单文件 1TB 上限，防恶意 size 声明
_MAX_WHE1_SIZE = 256 * 1024 * 1024 # WHE1 整块解密需全量进内存，超过此值拒收(防内存耗尽)
_RECV_IDLE_TIMEOUT = 300           # 接收 socket 空闲超时(秒)，防半开连接永久占住线程
_SEND_IO_TIMEOUT = 60              # 发送数据阶段单次 IO 超时(秒)
_DISK_MARGIN = 256 * 1024 * 1024   # 收完文件后磁盘至少还要剩这么多才接收
_ACK_OK = b"\x01"                  # 接收方回执：成功落盘
_ACK_FAIL = b"\x00"                # 接收方回执：失败(中断/解密失败/写盘失败)
_CHUNK_ENC_THRESHOLD = 32 * 1024 * 1024   # 加密文件超过此大小自动走 WHE2 分块(内存恒定)
_CONNECT_TIMEOUT = 10              # 单个地址的连接超时(多地址会逐个尝试)
_DRAIN_CAP = 8 * 1024 * 1024       # 拒收时最多帮对端消化这么多字节(让回执可靠到达)
_PROBE_INTERVAL = 5.0              # 对端存活探测间隔(秒)
_PROBE_TIMEOUT = 1.5               # 探测单个地址的 TCP 连接超时(秒)
_PROBE_STRIKES = 2                 # 连续失败这么多轮才判离线(防瞬时网络抖动误杀)
_NET_CHECK_INTERVAL = 5.0          # 网络监控轮询间隔(秒)
_NET_WAKE_GAP = 20.0               # 单轮 sleep 实际耗时超过此值判定为睡眠唤醒，触发 mDNS 重建


# ---------- 配置 ----------
@dataclass
class P2PConfig:
    inbox: str = "received"        # 收件箱：收到的文件落在这里
    listen_port: int = 0           # TCP 监听端口；0 = 操作系统自动分配
    peer_name: str = ""            # 本机显示名；空则用 hostname
    secret: str = ""               # 端到端加密口令(两台电脑必须一致；空=不加密)
    enable_mdns: bool = True       # False = 只起 TCP 不碰 mDNS(测试用，手动注册对端)
    trusted_only: bool = False     # True = 只接受当前选中目标设备的连接，其余拒收
    instance_id: str = ""          # 本机唯一实例 ID；持久化后同一设备重启不换服务名(见 P2PNode)

    def __post_init__(self):
        if not self.peer_name:
            self.peer_name = socket.gethostname()
        if not self.instance_id:
            self.instance_id = uuid.uuid4().hex[:8]


class PeerInfo:
    """一个已发现的对端节点。

    name         显示名(菜单里看到的；重名设备会带 " (2)" 后缀)
    host         主地址(显示用，也是首选连接地址)
    hosts        全部已知地址(多网卡/VPN 场景逐个尝试连接)
    service_name mDNS 完整服务名(唯一，用于离线事件精确匹配；手动注册可为空)
    """
    __slots__ = ("name", "host", "port", "service_name", "hosts")

    def __init__(self, name: str, host: str, port: int,
                 service_name: str = "", hosts: list[str] | None = None):
        self.name = name
        self.host = host
        self.port = port
        self.service_name = service_name
        self.hosts = [h for h in (hosts or []) if h]
        if host and host not in self.hosts:
            self.hosts.insert(0, host)

    def __repr__(self):
        return f"PeerInfo({self.name!r}, {self.host}:{self.port})"

    def __str__(self):
        return f"{self.name} ({self.host})"

    @property
    def instance_id(self) -> str:
        """从 service_name 提取 instance_id（最后 8 位十六进制）。
        service_name 格式：{label}-{instance_id}._inkhole._tcp.local.，无 service_name 返回空。"""
        if self.service_name and "-" in self.service_name:
            # service_name 格式: "V2419A-8980894b._inkhole._tcp.local."
            # 提取 "-" 后面到 "." 之前的部分
            part = self.service_name.rsplit("-", 1)[-1]
            # 去掉 "._inkhole._tcp.local." 后缀，只保留 instance_id
            if "." in part:
                return part.split(".", 1)[0]
            return part
        return ""


def _service_label(name: str, instance_id: str) -> str:
    """mDNS 服务实例标签：显示名 + 实例 ID 后缀，保证局域网内唯一。

    显示名里的 "." 会破坏服务名解析(DNS 标签分隔符)，替换掉；
    单个 DNS 标签最长 63 字节，utf-8 截断到 40 字节给后缀留余量。
    """
    label = name.replace(".", "-")
    raw = label.encode("utf-8")[:40]
    label = raw.decode("utf-8", errors="ignore")
    return f"{label}-{instance_id}"


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
                 on_peers_changed: Callable[[], None] | None = None,
                 on_progress: Callable[[str, str, int, int], None] | None = None):
        self.cfg = cfg
        self.on_sent = on_sent
        self.on_received = on_received
        self.on_status = on_status
        self.on_peers_changed = on_peers_changed
        self.on_progress = on_progress   # (kind:"send"/"recv", 文件名, 已传字节, 总字节)

        # 本节点唯一实例 ID：进服务名保证唯一(两台设备同名不再冲突)，
        # 进 TXT 属性用于"不发现自己"(比按显示名过滤可靠)。
        # 从 cfg 取(桌宠会持久化到 config.json)——同一设备重启用同一 ID，
        # 服务名不变，避免旧记录变成永不消失的"幽灵设备"。
        self._instance_id = cfg.instance_id or uuid.uuid4().hex[:8]

        self._peers: dict[str, PeerInfo] = {}   # 显示名 -> PeerInfo
        self._lock = threading.Lock()
        self._selected_peer: str | None = None  # 当前选中的目标
        self._last_selected_service: str | None = None  # 智能保留：记住选中设备的 service_name
        self._running = False

        # mDNS 相关
        self._zc = None              # Zeroconf 实例
        self._browser = None         # ServiceBrowser
        self._listener = None        # _InkHoleListener
        self._service_info = None    # 自己注册的 ServiceInfo
        self._actual_port = 0        # 实际监听端口
        # 网络监控：待机唤醒/换网后 mDNS 组播 socket 会失效，需重建(见 _net_monitor_loop)
        self._last_local_ips: list[str] = []   # 上次建 mDNS 时的本机 IP，用于检测变化
        self._net_monitor_thread: threading.Thread | None = None

        # TCP 服务器
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

        # 对端存活探测(幽灵设备兜底)；参数做成实例属性主要为了测试提速
        self._probe_interval = _PROBE_INTERVAL
        self._probe_timeout = _PROBE_TIMEOUT
        self._probe_strikes = _PROBE_STRIKES
        self._probe_thread: threading.Thread | None = None

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

        # 2. 对端存活探测线程(mDNS goodbye 丢包/对端崩溃的兜底，见 _probe_loop)
        self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
        self._probe_thread.start()

        # 3. 注册 mDNS 服务(测试模式跳过：只测 TCP 层，不受局域网环境干扰)
        if not self.cfg.enable_mdns:
            self._status(f"墨洞已开启(无 mDNS) · {self.cfg.peer_name} @ 127.0.0.1:{self._actual_port}")
            return

        self._setup_mdns()

        # 4. 网络监控线程：待机唤醒/换网后重建 mDNS(见 _net_monitor_loop)
        self._net_monitor_thread = threading.Thread(target=self._net_monitor_loop, daemon=True)
        self._net_monitor_thread.start()

        local_ips = self._last_local_ips or ["127.0.0.1"]
        self._status(f"墨洞已开启 · {self.cfg.peer_name} @ {local_ips[0]}:{self._actual_port}")

    def _setup_mdns(self) -> None:
        """创建 Zeroconf 实例、注册本机服务、启动发现浏览器。

        抽成独立方法是为了待机唤醒/换网后能整段拆掉重建——
        zeroconf 的组播 socket 绑在建实例时存在的网卡上，网卡被拆再挂
        (睡眠唤醒、切 WiFi、DHCP 换租)后旧 socket 变死连接，必须重建。
        """
        try:
            from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser
        except ImportError:
            raise SystemExit("P2P 模式需要 zeroconf 库：pip install zeroconf")

        local_ips = self._get_local_ips()
        self._last_local_ips = local_ips
        self._zc = Zeroconf()
        self._service_info = ServiceInfo(
            type_=_SERVICE_TYPE,
            name=f"{_service_label(self.cfg.peer_name, self._instance_id)}.{_SERVICE_TYPE}",
            # 宣告全部本机 IPv4(默认路由地址排最前)：开 VPN/多网卡时
            # 对端能拿到局域网真实地址逐个尝试，而不是只有 VPN 虚拟地址
            addresses=[socket.inet_aton(ip) for ip in local_ips],
            port=self._actual_port,
            properties={
                b"peer_name": self.cfg.peer_name.encode("utf-8"),
                b"instance_id": self._instance_id.encode("ascii"),
            },
        )
        # 服务名已带唯一后缀，理论上不会撞名；万一撞了让 zeroconf 自动改名而不是崩溃
        self._zc.register_service(self._service_info, allow_name_change=True)

        # 开始发现其他节点
        self._listener = _InkHoleListener(self)
        self._browser = ServiceBrowser(self._zc, _SERVICE_TYPE, self._listener)

    def _teardown_mdns(self) -> None:
        """注销并关闭当前 mDNS 层(浏览器 + 服务 + Zeroconf 实例)。"""
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
        self._service_info = None
        self._listener = None

    def stop(self) -> None:
        """停止：注销 mDNS + 关闭 TCP 监听。"""
        self._running = False
        self._teardown_mdns()
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
        """接收一个文件：读 WHPP 头 + 文件数据，落盘到收件箱。

        任何失败(中断/解密失败/写盘失败)都不允许把残缺文件顶着正式文件名
        落盘——否则半截文件会覆盖收件箱里同名的完整文件。
        对端 header 带 want_ack 时，结束前回 1 字节回执告知成败；
        拒收路径先把对端已发出的数据消化掉(有限额)，回执才能可靠到达。
        """
        part_path = None
        want_ack = False
        ok = False
        try:
            # 空闲超时：对端发一半停住不能永久占住线程和 .part 文件
            conn.settimeout(_RECV_IDLE_TIMEOUT)
            # 仅接收目标设备：来源 IP 不是当前选中设备的地址就直接拒
            if self.cfg.trusted_only:
                with self._lock:
                    sel = self._peers.get(self._selected_peer) if self._selected_peer else None
                    allowed = set(sel.hosts) if sel else set()
                if addr[0] not in allowed:
                    self._status(f"已拒收 {addr[0]} 的连接（仅接收目标设备）")
                    try:
                        conn.sendall(_ACK_FAIL)
                    except OSError:
                        pass
                    _drain(conn, _DRAIN_CAP)
                    return

            # 读 magic
            magic = _recv_exact(conn, 4)
            if magic != _MAGIC:
                return

            # 读 header 长度 + header JSON
            hdr_len_bytes = _recv_exact(conn, 4)
            if not hdr_len_bytes:
                return
            hdr_len = struct.unpack("!I", hdr_len_bytes)[0]
            if not 0 < hdr_len <= _MAX_HEADER:
                return
            hdr_bytes = _recv_exact(conn, hdr_len)
            if not hdr_bytes:
                return
            header = json.loads(hdr_bytes.decode("utf-8"))

            filename = _safe_filename(str(header.get("filename", "")))  # 防 ../ 路径穿越 + 非法字符
            size = header.get("size", 0)
            encrypted = bool(header.get("encrypted", False))
            enc_mode = str(header.get("enc_mode", ""))
            want_ack = bool(header.get("want_ack", False))

            # size 来自网络，不可信：必须是合法范围内的整数
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_FILE_SIZE:
                self._status(f"拒收 {filename}：文件大小非法")
                return
            # 磁盘装不下就直接拒收，别收到一半才发现
            try:
                if size + _DISK_MARGIN > shutil.disk_usage(self.cfg.inbox).free:
                    self._status(f"拒收 {filename}：磁盘空间不足")
                    _drain(conn, min(size, _DRAIN_CAP))
                    return
            except OSError:
                pass
            # 对方加密但本机没口令：内容永远解不开，收下没有意义
            if encrypted and not self.cfg.secret:
                self._status(f"拒收 {filename}：对方启用了加密，本机未设口令")
                _drain(conn, min(size, _DRAIN_CAP))
                return
            # WHE1 整块解密需把密文全量读进内存，超大声明是内存耗尽攻击
            if encrypted and enc_mode != "chunked" and size > _MAX_WHE1_SIZE:
                self._status(f"拒收 {filename}：整块加密文件过大")
                _drain(conn, _DRAIN_CAP)
                return

            # 落盘绝不覆盖已有文件；.part 带随机后缀，并发收同名文件互不干扰
            dst = _unique_path(self.cfg.inbox, filename)
            part_path = dst + f".{uuid.uuid4().hex[:8]}.part"
            progress = _Progress(self.on_progress, "recv", filename, size)

            if encrypted and enc_mode == "chunked":
                # WHE2 分块流：边收边解密边落盘，内存峰值 4MB
                hdr32 = _recv_exact(conn, 32)
                if hdr32 is None:
                    self._status(f"接收中断：{filename}")
                    return
                try:
                    decryptor = ChunkedDecryptor(self.cfg.secret, hdr32)
                except ValueError:
                    self._status(f"拒收 {filename}：加密流头非法")
                    return
                consumed = 32
                intact = True
                with open(part_path, "wb") as f:
                    while consumed < size:
                        len_bytes = _recv_exact(conn, 4)
                        if len_bytes is None:
                            intact = False
                            break
                        ct_len = struct.unpack("!I", len_bytes)[0]
                        if not 16 <= ct_len <= CHUNK_SIZE + 16:
                            intact = False
                            break
                        ct = _recv_exact(conn, ct_len)
                        if ct is None:
                            intact = False
                            break
                        plain = decryptor.decrypt_chunk(ct)
                        if plain is None:
                            self._status(f"解密失败：{filename}（两端口令不一致？）")
                            return
                        f.write(plain)
                        consumed += 4 + ct_len
                        progress.update(consumed)
                if not intact or consumed != size:
                    self._status(f"接收中断：{filename}")
                    return
            else:
                # 明文 / WHE1 整块加密：流式写入 .part
                remaining = size
                with open(part_path, "wb") as f:
                    while remaining > 0:
                        chunk = conn.recv(min(_BUFFER, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                        progress.update(size - remaining)
                if remaining > 0:
                    self._status(f"接收中断：{filename}")
                    return

                # WHE1 整块加密：解密成功才算收到
                if encrypted:
                    with open(part_path, "rb") as f:
                        blob = f.read()
                    plain = decrypt(self.cfg.secret, blob)
                    if plain is None:
                        self._status(f"解密失败：{filename}（两端口令不一致？）")
                        return
                    with open(part_path, "wb") as f:
                        f.write(plain)

            # 落盘时在锁内再取一次唯一名：并发收同名文件时先落盘的不被后来的覆盖
            with self._lock:
                dst = _unique_path(self.cfg.inbox, filename)
                os.replace(part_path, dst)
            part_path = None
            ok = True

            if self.on_received:
                self.on_received(dst)
        except Exception as e:
            self._status("接收失败", str(e))
        finally:
            if want_ack:
                try:
                    conn.sendall(_ACK_OK if ok else _ACK_FAIL)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
            if part_path and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    # ---------- 发送文件 ----------
    def send_file(self, local_path: str) -> bool:
        """把文件直接发给选中的对端。成功返回 True。

        header 带 want_ack：新版接收方处理完回 1 字节回执，落盘失败能被
        发送方感知；老版接收方(v1.0.0)读完数据直接关连接，按成功处理。
        """
        if not os.path.isfile(local_path):
            return False

        with self._lock:
            peer = self._peers.get(self._selected_peer) if self._selected_peer else None
        if not peer:
            self._status("请先右键选择目标设备")
            return False

        name = os.path.basename(local_path)
        try:
            plain_size = os.path.getsize(local_path)
            enc_mode = ""
            data = None
            if self.cfg.secret and plain_size > _CHUNK_ENC_THRESHOLD:
                # 大文件走 WHE2 分块流式加密：内存峰值 4MB，不再整块读入
                encrypted = True
                enc_mode = "chunked"
                file_size = chunked_wire_size(plain_size)
            elif self.cfg.secret:
                # 小文件保持 WHE1 整块(与所有旧版本互通)
                with open(local_path, "rb") as f:
                    data = encrypt(self.cfg.secret, f.read())
                encrypted = True
                file_size = len(data)
            else:
                encrypted = False
                file_size = plain_size

            hdr = {
                "filename": name,
                "size": file_size,
                "encrypted": encrypted,
                "want_ack": True,
            }
            if enc_mode:
                hdr["enc_mode"] = enc_mode
            header = json.dumps(hdr).encode("utf-8")

            progress = _Progress(self.on_progress, "send", name, file_size)

            # 多网卡/VPN 场景：逐个地址尝试，先通先用
            sock = None
            last_err: OSError | None = None
            for host in (peer.hosts or [peer.host]):
                try:
                    sock = socket.create_connection((host, peer.port),
                                                    timeout=_CONNECT_TIMEOUT)
                    break
                except OSError as e:
                    last_err = e
            if sock is None:
                raise last_err if last_err else OSError("无可用地址")

            try:
                # create_connection 的 10s 连接超时会留在 socket 上，数据阶段
                # 放宽到 60s——接收方磁盘偶发卡顿不该被误判成发送失败
                sock.settimeout(_SEND_IO_TIMEOUT)
                sock.sendall(_MAGIC)
                sock.sendall(struct.pack("!I", len(header)))
                sock.sendall(header)

                sent = 0
                if enc_mode == "chunked":
                    # 边读边加密边发，恒定内存
                    with open(local_path, "rb") as f:
                        for blob in encrypt_chunks(self.cfg.secret, f):
                            sock.sendall(blob)
                            sent += len(blob)
                            progress.update(sent)
                elif data is not None:
                    # 加密数据已在内存，分块发送
                    while sent < len(data):
                        sock.sendall(data[sent:sent + _BUFFER])
                        sent = min(sent + _BUFFER, len(data))
                        progress.update(sent)
                else:
                    # 明文：流式从磁盘读
                    with open(local_path, "rb") as f:
                        while True:
                            chunk = f.read(_BUFFER)
                            if not chunk:
                                break
                            sock.sendall(chunk)
                            sent += len(chunk)
                            progress.update(sent)

                # 等接收方回执。老版本对端读完即关连接 -> recv 返回 b""，按成功；
                # 超时(对端解密大文件等)也不误报失败。
                sock.settimeout(60)
                try:
                    resp = sock.recv(1)
                except (socket.timeout, OSError):
                    resp = b""
                if resp == _ACK_FAIL:
                    self._status(f"{peer.name} 接收失败（口令不一致、被拒收或磁盘问题）")
                    return False
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
            if name is None:
                self._selected_peer = None
                self._last_selected_service = None
            elif name in self._peers:
                self._selected_peer = name
                # 智能保留：记住 service_name，离线后重新上线能自动恢复选中
                self._last_selected_service = self._peers[name].service_name
        label = name if name else "未选择"
        self._status(f"目标: {label}")

    def _on_peer_added(self, name: str, host: str, port: int, service_name: str = "",
                       hosts: list[str] | None = None) -> None:
        """mDNS 发现新节点时调用（由 _InkHoleListener 触发）。

        - 同一服务(service_name 相同)重复通告/地址变化：原地更新，不新增条目。
        - 不同设备撞了显示名：给后来者加 " (2)" 后缀，两台都能选。
        - 智能保留：若新上线设备的 service_name 匹配之前选中的，自动恢复选择。
        """
        display_name = name  # 最终显示名（可能带后缀）
        with self._lock:
            updated = False
            if service_name:
                for p in self._peers.values():
                    if p.service_name == service_name:
                        fresh = PeerInfo(p.name, host, port, service_name, hosts)
                        p.host, p.port, p.hosts = fresh.host, fresh.port, fresh.hosts
                        display_name = p.name
                        updated = True
                        break
            if not updated:
                display = name
                n = 2
                while display in self._peers and self._peers[display].service_name != service_name:
                    display = f"{name} ({n})"
                    n += 1
                self._peers[display] = PeerInfo(display, host, port, service_name, hosts)
                display_name = display

            # 智能保留：若此设备的 service_name 匹配之前选中的，自动恢复选择
            if (service_name and service_name == self._last_selected_service
                    and self._selected_peer is None):
                self._selected_peer = display_name
                self._status(f"目标设备 {display_name} 重新上线，已自动恢复选中")

        if not updated:
            self._status(f"发现 {name}")

        if self.on_peers_changed:
            self.on_peers_changed()

    def _on_peer_removed(self, name: str) -> None:
        """按显示名移除节点(离线)。

        智能保留：离线时清空 _selected_peer（避免向已离线设备发送），
        但保留 _last_selected_service，等对端重新上线时自动恢复选中。
        """
        with self._lock:
            if name in self._peers:
                del self._peers[name]
            if self._selected_peer == name:
                self._selected_peer = None    # 清空当前选择，但保留 _last_selected_service

        self._status(f"{name} 离线")

        if self.on_peers_changed:
            self.on_peers_changed()

    def _on_peer_removed_by_service(self, service_name: str) -> None:
        """mDNS 节点离线时调用：按唯一服务名精确匹配，找不到再回退按名字解析。

        显示名可能被去重加过后缀、或与服务名前缀不一致(TXT 里的 peer_name)，
        只有 service_name 能可靠对上离线事件，否则会留下永不消失的幽灵设备。
        """
        display = None
        with self._lock:
            for n, p in self._peers.items():
                if p.service_name == service_name:
                    display = n
                    break
        if display is None:
            # 老版本对端/手动注册的条目：回退按服务名前缀解析显示名
            suffix = "." + _SERVICE_TYPE
            display = service_name[:-len(suffix)] if service_name.endswith(suffix) \
                else service_name.split(".")[0]
        self._on_peer_removed(display)

    # ---------- 对端存活探测 ----------
    def _probe_loop(self) -> None:
        """幽灵设备兜底：定期 TCP 探测已发现的对端，连不上的剔除。

        mDNS 的离线通告(goodbye)走 UDP 组播，WiFi 下经常丢；对端崩溃/被
        强杀更是根本不会发。只靠 remove_service 回调，幽灵设备要挂到 PTR
        记录 TTL(默认 75 分钟)过期才消失。而对端进程一退监听端口就关了，
        TCP connect 立刻失败——比等 mDNS 可靠得多。探测连上即断，接收端
        读不到 magic 会静默丢弃这条空连接(见 _handle_connection)。
        """
        strikes: dict[str, int] = {}   # service_name(或显示名) -> 连续失败轮数
        while self._running:
            time.sleep(self._probe_interval)
            if not self._running:
                break
            with self._lock:
                targets = [(p.service_name or n, list(p.hosts), p.port, n)
                           for n, p in self._peers.items()]
            seen = set()
            for key, hosts, port, display in targets:
                seen.add(key)
                alive = False
                for host in hosts:
                    try:
                        socket.create_connection((host, port),
                                                 timeout=self._probe_timeout).close()
                        alive = True
                        break
                    except OSError:
                        continue
                if alive:
                    strikes.pop(key, None)
                elif strikes.get(key, 0) + 1 >= self._probe_strikes:
                    strikes.pop(key, None)
                    self._on_peer_removed(display)
                else:
                    strikes[key] = strikes.get(key, 0) + 1
            # 已经不在对端表里的条目不再计数(正常离线/被 mDNS 先移除)
            for k in list(strikes):
                if k not in seen:
                    del strikes[k]

    # ---------- 网络变化监控 ----------
    def _net_monitor_loop(self) -> None:
        """待机唤醒 / 换网后重建 mDNS 层。

        zeroconf 的组播 socket 绑在建实例时的网卡上。Windows 睡眠会拆掉
        网卡、唤醒后重挂(DHCP 还可能换 IP)，旧 socket 变成死连接：既发现
        不了新设备，自己也广播不出去，表现为"待机后搜不到设备"。TCP 监听层
        不受影响，所以只重建 mDNS，不动 TCP。

        两种触发：
          1. 睡眠唤醒——本线程随进程一起被挂起，唤醒后这一轮 sleep 的实际
             耗时会远超 _NET_CHECK_INTERVAL，据此判定。
          2. 本机 IP 变化——切 WiFi/插网线/DHCP 换租，即使没睡眠也要重建。
        """
        last_tick = time.monotonic()
        while self._running:
            time.sleep(_NET_CHECK_INTERVAL)
            if not self._running:
                break
            now = time.monotonic()
            gap = now - last_tick
            last_tick = now

            woke = gap > _NET_WAKE_GAP
            ips_now = self._get_local_ips()
            ip_changed = ips_now != self._last_local_ips

            if woke or ip_changed:
                reason = "睡眠唤醒" if woke else "网络变化"
                self._rebuild_mdns(reason)
                # 重建自身耗时可能不短，重置基准避免把它误判成又一次唤醒
                last_tick = time.monotonic()

    def _rebuild_mdns(self, reason: str) -> None:
        """拆掉并重新创建整个 mDNS 层(唤醒/换网后自愈)。

        对端表保留：浏览器重建后会重新收到在线设备的通告刷新地址，
        探测线程会清理掉真正够不着的旧设备，不必在这里清空(清空会让
        菜单瞬间空掉、还丢掉当前选中)。
        """
        if not self._running or not self.cfg.enable_mdns:
            return
        self._status(f"检测到{reason}，正在重建设备发现…")
        try:
            self._teardown_mdns()
        except Exception:
            pass
        try:
            self._setup_mdns()
            ip = (self._last_local_ips or ["?"])[0]
            self._status(f"设备发现已重建 · {ip}:{self._actual_port}")
        except Exception as e:
            self._status("设备发现重建失败", str(e))

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

    def _get_local_ips(self) -> list[str]:
        """本机全部非环回 IPv4，默认路由地址排最前，过滤虚拟网卡。

        排除 VMware/VirtualBox/Hyper-V 等虚拟网卡，避免 mDNS 广播不可达地址。
        Android NSD API 的 host 字段只返回一个 IP，若拿到虚拟网卡地址会连接失败。
        """
        ips = [self._get_local_ip()]
        try:
            # 优先用 psutil 获取网卡详细信息，按名称过滤虚拟网卡
            import psutil
            virtual_keywords = ("vmware", "virtualbox", "vbox", "hyper-v", "vethernet",
                                "docker", "vmmem", "wsl")
            for iface, addrs in psutil.net_if_addrs().items():
                if any(kw in iface.lower() for kw in virtual_keywords):
                    continue  # 跳过虚拟网卡
                for addr in addrs:
                    if addr.family == socket.AF_INET:
                        ip = addr.address
                        # 排除环回、APIPA 链路本地地址(169.254.x.x 通常不可路由)
                        if (ip not in ips and not ip.startswith("127.")
                                and not ip.startswith("169.254.")):
                            ips.append(ip)
        except ImportError:
            # psutil 未安装，回退到基础过滤：排除 169.254.x.x (APIPA) 和常见虚拟网段
            for _fam, _t, _p, _c, sockaddr in socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET):
                ip = sockaddr[0]
                if (ip not in ips and not ip.startswith("127.")
                        and not ip.startswith("169.254.")  # Windows APIPA 自动分配
                        and not ip.startswith("192.168.56.")  # VirtualBox 默认
                        and not ip.startswith("192.168.99.")):  # Docker Machine
                    ips.append(ip)
        except (socket.gaierror, OSError):
            pass
        return ips

    @property
    def actual_port(self) -> int:
        return self._actual_port


# ---------- mDNS 监听器 ----------
class _InkHoleListener:
    """zeroconf ServiceBrowser 回调：发现/离线时更新 P2PNode 的对端表。"""

    def __init__(self, node: P2PNode):
        self._node = node

    def _upsert(self, zc, type_: str, name: str) -> None:
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
            # 回退：从服务名 "MyPC._inkhole._tcp.local." 提取 "MyPC"
            peer_name = name.split(".")[0] if name else "unknown"

        # 不添加自己：按实例 ID 判断(可靠)；老版本对端无 instance_id，
        # 回退按显示名判断(与 v1.0.0 行为一致)
        instance_id = props.get("instance_id", "")
        if instance_id:
            if instance_id == self._node._instance_id:
                return
        elif peer_name == self._node.cfg.peer_name:
            return

        addresses = info.parsed_addresses()
        if not addresses:
            return

        # 兜底自我过滤：同名 + 地址全落在本机 IP 上，判定为自己的历史注册
        # (进程曾用旧 instance_id 注册、goodbye 丢包残留)，丢弃不显示。
        if peer_name == self._node.cfg.peer_name:
            local_ips = set(self._node._get_local_ips()) | {"127.0.0.1"}
            if all(a in local_ips for a in addresses):
                return

        self._node._on_peer_added(peer_name, addresses[0], info.port,
                                  service_name=name, hosts=list(addresses))

    def add_service(self, zc, type_: str, name: str) -> None:
        self._upsert(zc, type_, name)

    def remove_service(self, zc, type_: str, name: str) -> None:
        self._node._on_peer_removed_by_service(name)

    def update_service(self, zc, type_: str, name: str) -> None:
        # 对端 IP/端口变化(DHCP 换租、重启换端口)时刷新，否则发送会连旧地址
        self._upsert(zc, type_, name)


# ---------- 文件名安全 ----------
def _safe_filename(raw: str) -> str:
    """把来自网络的文件名清洗成能安全落盘的名字。

    - basename 双向裁剪(/ 和 \\ 都算分隔符，Linux 收 Windows 发的名字也不穿越)
    - 替换 Windows 非法字符(其中 ":" 在 NTFS 上会写进备用数据流)
    - 去掉尾部点/空格(Windows 不允许)，空名回退 unknown
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in name)
    name = name.rstrip(". ")
    if name in ("", ".", ".."):
        return "unknown"
    return name


def _unique_path(directory: str, filename: str) -> str:
    """收件箱内不重名的目标路径：已存在则加 " (2)"、" (3)"… 后缀。

    收到的文件绝不覆盖已有文件——否则局域网内任何设备都能用同名文件
    静默替换你已收到的内容。"""
    dst = os.path.join(directory, filename)
    if not os.path.exists(dst):
        return dst
    stem, ext = os.path.splitext(filename)
    n = 2
    while True:
        dst = os.path.join(directory, f"{stem} ({n}){ext}")
        if not os.path.exists(dst):
            return dst
        n += 1


def _zip_dir(src_dir: str) -> str:
    """把目录递归打包成一个临时 zip，返回临时 zip 绝对路径。

    zip 名为 <目录basename>.zip，落在独立临时目录里（调用方发送后应删除
    整个临时目录）。arcname 用相对 src_dir 的路径，保持目录结构；空目录
    也生成合法（空）zip。用 ZIP_DEFLATED 压缩。
    """
    import zipfile
    src_dir = os.path.abspath(src_dir)
    base = os.path.basename(src_dir.rstrip("/\\")) or "folder"
    tmp_root = tempfile.mkdtemp(prefix="inkhole_zip_")
    zip_path = os.path.join(tmp_root, base + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, src_dir)
                z.write(full, arc)
            # 显式写目录条目（含空子目录），保持完整目录结构
            for d in dirs:
                full_d = os.path.join(root, d)
                arc_d = os.path.relpath(full_d, src_dir).replace("\\", "/") + "/"
                z.writestr(zipfile.ZipInfo(arc_d), "")
    return zip_path


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


def _drain(sock: socket.socket, n: int) -> None:
    """拒收时把对端已发出的最多 n 字节读掉再关连接。

    不读就 close 会触发 RST，可能冲掉已排队的 ACK_FAIL 回执，
    发送方只能看到"连接错误"而不是"对方拒收"。读到 EOF/超时即止。"""
    try:
        sock.settimeout(10)
        left = n
        while left > 0:
            chunk = sock.recv(min(_BUFFER, left))
            if not chunk:
                return
            left -= len(chunk)
    except OSError:
        pass


class _Progress:
    """传输进度节流上报：最多每 0.25s 回调一次，完成时必回调。

    回调签名 on_progress(kind, filename, done, total)，kind = "send"/"recv"。
    回调里抛异常不打断传输。
    """
    __slots__ = ("_cb", "_kind", "_name", "_total", "_last")

    def __init__(self, cb, kind: str, name: str, total: int):
        self._cb = cb
        self._kind = kind
        self._name = name
        self._total = total
        self._last = 0.0

    def update(self, done: int) -> None:
        if self._cb is None:
            return
        now = time.monotonic()
        if done < self._total and now - self._last < 0.25:
            return
        self._last = now
        try:
            self._cb(self._kind, self._name, done, self._total)
        except Exception:
            pass


# ---------- 命令行入口 ----------
def _run_cli(argv=None) -> None:
    """无 GUI 模式：监视发件箱自动发送 + 收件箱自动接收。"""
    import argparse
    ap = argparse.ArgumentParser(description="墨洞 P2P(命令行版，无动画)")
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
        on_sent=lambda n: print(f"[发送] {n} 已吸入墨洞"),
        on_received=lambda p: print(f"[接收] 墨洞吐出 -> {p}"),
        on_status=lambda s: print(f"[状态] {s}"),
        on_peers_changed=lambda: print(f"[设备] {', '.join(node.peer_names()) or '无'}"),
        on_progress=lambda kind, name, done, total: print(
            f"[{'↑' if kind == 'send' else '↓'}] {name} "
            f"{done * 100 // total if total else 100}% ({done}/{total})"),
    )
    node.start()
    print(f"墨洞 P2P 已启动 · 收件箱={os.path.abspath(args.inbox)}")

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
