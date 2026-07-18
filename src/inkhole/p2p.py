"""
p2p.py
======
墨洞 P2P 引擎：局域网点对点文件传输，无需服务器。

架构：
  - mDNS (zeroconf) 自动发现局域网内其他墨洞节点
  - 直接 TCP 连接传输文件，无中转、无云端
  - 可选 AES-256-GCM 端到端加密(复用 crypto.py)

协议 (WHPP - InkHole P2P Protocol)：
  [4B magic "WHPP"] [4B header_len] [header_len B JSON] [size B 数据]
  普通文件直接承载字节；kind=folder-v1 时承载 WHF1 目录条目流。
  WHPC 独立连接用于能力探测，旧客户端自动回退 ZIP。

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
import stat
import struct
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .crypto import (encrypt, decrypt, is_encrypted, encrypt_chunks,
                     chunked_wire_size, ChunkedDecryptor, CHUNK_SIZE)

# ---------- 常量 ----------
_SERVICE_TYPE = "_inkhole._tcp.local."
_MAGIC = b"WHPP"          # InkHole P2P Protocol magic
_CAP_MAGIC = b"WHPC"      # capability probe; kept separate from file frames
_FOLDER_MAGIC = b"WHF1"   # streamed folder payload magic
_FOLDER_KIND = "folder-v1"
_FOLDER_ENTRY = struct.Struct("!BIQQ")  # type, path bytes, file size, mtime ms
_BUFFER = 256 * 1024      # 256KB 传输块，降低大文件跨网传输的 Python IO 调用开销
_SOCKET_BUFFER = 4 * 1024 * 1024   # TCP 窗口上限:4MB @ RTT 200ms(DERP) ≈ 20MB/s,
                                   # @ RTT 60ms(WiFi 抖动) ≈ 66MB/s,高于链路真实能力
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
_CAP_TIMEOUT = 3.0                  # folder-v1 能力探测读写超时
_MAX_FOLDER_ENTRIES = 100_000       # 防恶意条目数耗尽 inode/内存
_MAX_FOLDER_PATH = 4096             # 单条 UTF-8 相对路径字节上限
_MAX_FOLDER_DEPTH = 128             # 防超深目录拖垮路径处理
_PORTABLE_INVALID = '<>:"|?*'
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class _SendCancelled(Exception):
    pass


def _tune_transfer_socket(sock: socket.socket) -> None:
    """放大 TCP 收发缓冲。必须在 bind(服务端)/connect(客户端)之前调用：
    窗口缩放因子在握手时按当时的缓冲协商，连接建立后再放大只改本地队列、
    不改窗口上限；且显式设置会禁用系统自动调优，设晚了反而把窗口钉死。"""
    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, _SOCKET_BUFFER)
        except OSError:
            pass


def _is_cgnat_ip(host: str) -> bool:
    """100.64.0.0/10(运营商 CGNAT 段,Tailscale 用它分配虚拟 IP)。"""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def _cgnat_source_ip() -> str | None:
    """本机 Tailscale 接口的 100.x 地址;Tailscale 不在线返回 None。"""
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET and _is_cgnat_ip(addr.address):
                    return addr.address
    except ImportError:
        try:
            for *_ignored, sockaddr in socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET):
                if _is_cgnat_ip(sockaddr[0]):
                    return sockaddr[0]
        except (socket.gaierror, OSError):
            pass
    except OSError:
        pass
    return None


def _probe_connect(host: str, port: int, timeout: float) -> None:
    """探活连接(连上即断,失败抛 OSError)。100.x(Tailscale)目标强制从
    本机 Tailscale 接口出发,接口不在线直接判不可达——否则 connect 按
    默认路由泄漏(被代理 TUN 假 accept / 进运营商 CGNAT),对端明明下线
    探活却一直"成功",设备永远赖在列表里。"""
    if _is_cgnat_ip(host):
        src = _cgnat_source_ip()
        if src is None:
            raise OSError("Tailscale 接口不在线")
        socket.create_connection((host, port), timeout=timeout,
                                 source_address=(src, 0)).close()
    else:
        socket.create_connection((host, port), timeout=timeout).close()


def _connect_transfer_socket(host: str, port: int, timeout: float) -> socket.socket:
    """按传输要求建立发送连接(缓冲在 connect 前设置，见 _tune_transfer_socket)。"""
    err: OSError | None = None
    for af, socktype, proto, _c, sa in socket.getaddrinfo(
            host, port, 0, socket.SOCK_STREAM):
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            _tune_transfer_socket(sock)
            # Tailscale 目标必须从 Tailscale 接口出发(见 _probe_connect)
            if af == socket.AF_INET and _is_cgnat_ip(sa[0]):
                src = _cgnat_source_ip()
                if src is None:
                    raise OSError("Tailscale 接口不在线")
                sock.bind((src, 0))
            sock.settimeout(timeout)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    raise err if err is not None else OSError(f"无法解析地址 {host}")


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
    # 手动添加的设备(Tailscale/固定 IP 直连用)：mDNS 组播不穿虚拟网卡,
    # 这些设备靠探测线程维持在线状态。元素: {"name": str, "host": str, "port": int}
    manual_peers: list = field(default_factory=list)

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


@dataclass(frozen=True)
class _FolderEntryInfo:
    path: str
    path_bytes: bytes
    source: str
    is_dir: bool
    size: int
    mtime_ms: int


@dataclass(frozen=True)
class _FolderManifest:
    root: str
    entries: tuple[_FolderEntryInfo, ...]
    plain_size: int
    root_mtime_ms: int


def _portable_path_parts(path: str) -> tuple[list[str], str]:
    """Validate a WHF1 relative path and return components plus collision key."""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("文件夹内含不安全路径")
    parts = path.split("/")
    if len(parts) > _MAX_FOLDER_DEPTH:
        raise ValueError("文件夹目录层级过深")
    keys = []
    for component in parts:
        encoded = component.encode("utf-8")
        if (not component or component in (".", "..") or len(encoded) > 255
                or component.rstrip(". ") != component
                or any(ord(ch) < 32 or ch in _PORTABLE_INVALID for ch in component)
                or component.split(".", 1)[0].upper() in _WINDOWS_RESERVED):
            raise ValueError(f"文件夹内含跨平台不支持的名称：{component or '?'}")
        keys.append(unicodedata.normalize("NFC", component).casefold())
    return parts, "/".join(keys)


def _is_reparse_point(st_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(st_result, "st_file_attributes", 0) & flag)


def _scan_folder(path: str,
                 should_cancel: Callable[[], bool] | None = None) -> _FolderManifest:
    """Scan once and retain the exact metadata needed to stream a folder."""
    root = os.path.abspath(path)
    root_stat = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(root_stat.st_mode) or os.path.islink(root) or _is_reparse_point(root_stat):
        raise ValueError("不支持发送符号链接、联接或特殊目录")

    entries: list[_FolderEntryInfo] = []
    collision_keys: set[str] = set()
    plain_size = 8  # WHF1 magic + entry_count
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        if should_cancel and should_cancel():
            raise _SendCancelled()
        current, relative_parent = stack.pop()
        with os.scandir(current) as scan:
            children = sorted(scan, key=lambda item: item.name.casefold())
        directories: list[tuple[str, str]] = []
        for child in children:
            if should_cancel and should_cancel():
                raise _SendCancelled()
            relative = f"{relative_parent}/{child.name}" if relative_parent else child.name
            path_bytes = relative.encode("utf-8")
            if len(path_bytes) > _MAX_FOLDER_PATH:
                raise ValueError(f"文件夹内路径过长：{relative}")
            _parts, collision_key = _portable_path_parts(relative)
            if collision_key in collision_keys:
                raise ValueError(f"文件夹内存在跨平台重名路径：{relative}")
            collision_keys.add(collision_key)

            st_result = child.stat(follow_symlinks=False)
            if child.is_symlink() or _is_reparse_point(st_result):
                raise ValueError(f"不支持发送符号链接或联接：{relative}")
            mtime_ms = min(max(0, st_result.st_mtime_ns // 1_000_000), 0xFFFFFFFFFFFFFFFF)
            if stat.S_ISDIR(st_result.st_mode):
                entry = _FolderEntryInfo(relative, path_bytes, child.path, True, 0, mtime_ms)
                directories.append((child.path, relative))
            elif stat.S_ISREG(st_result.st_mode):
                entry = _FolderEntryInfo(
                    relative, path_bytes, child.path, False, st_result.st_size, mtime_ms)
            else:
                raise ValueError(f"不支持发送特殊文件：{relative}")
            entries.append(entry)
            if len(entries) > _MAX_FOLDER_ENTRIES:
                raise ValueError("文件夹条目过多")
            plain_size += _FOLDER_ENTRY.size + len(path_bytes) + entry.size
            if plain_size > _MAX_FILE_SIZE:
                raise ValueError("文件夹总大小超过 1TB")
        stack.extend(reversed(directories))

    root_mtime_ms = min(max(0, root_stat.st_mtime_ns // 1_000_000), 0xFFFFFFFFFFFFFFFF)
    return _FolderManifest(root, tuple(entries), plain_size, root_mtime_ms)


class _FolderPayloadReader:
    """File-like reader over a WHF1 manifest without building an archive."""

    def __init__(self, manifest: _FolderManifest):
        self._pieces = self._iter_pieces(manifest)
        self._buffer = bytearray()
        self._closed = False

    @staticmethod
    def _iter_pieces(manifest: _FolderManifest):
        yield _FOLDER_MAGIC + struct.pack("!I", len(manifest.entries))
        for entry in manifest.entries:
            yield _FOLDER_ENTRY.pack(
                0 if entry.is_dir else 1,
                len(entry.path_bytes),
                entry.size,
                entry.mtime_ms,
            )
            yield entry.path_bytes
            if entry.is_dir:
                continue
            remaining = entry.size
            with open(entry.source, "rb") as source:
                while remaining:
                    chunk = source.read(min(_BUFFER, remaining))
                    if not chunk:
                        raise OSError(f"发送时文件发生变化：{entry.path}")
                    remaining -= len(chunk)
                    yield chunk
                if source.read(1):
                    raise OSError(f"发送时文件大小发生变化：{entry.path}")

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if size == 0:
            return b""
        if size < 0:
            chunks = [bytes(self._buffer)]
            self._buffer.clear()
            chunks.extend(self._pieces)
            return b"".join(chunks)
        while len(self._buffer) < size:
            try:
                self._buffer.extend(next(self._pieces))
            except StopIteration:
                break
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        self._closed = True
        self._buffer.clear()
        try:
            self._pieces.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _FolderWireReader:
    """Expose an exact WHF1 plaintext stream from clear or WHE2 wire data."""

    def __init__(self, conn: socket.socket, wire_size: int, plain_size: int,
                 secret: str, encrypted: bool, progress):
        self._conn = conn
        self._wire_size = wire_size
        self._plain_size = plain_size
        self._progress = progress
        self._wire_read = 0
        self._plain_read = 0
        self._encrypted = encrypted
        self._plain_buffer = bytearray()
        self._decryptor = None
        if encrypted:
            header = _recv_exact(conn, 32)
            if header is None:
                raise EOFError("加密流头不完整")
            self._wire_read = 32
            self._progress.update(self._wire_read)
            self._decryptor = ChunkedDecryptor(secret, header)

    def _read_clear(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._conn.recv(min(_BUFFER, size - len(chunks)))
            if not chunk:
                raise EOFError("文件夹数据不完整")
            chunks.extend(chunk)
            self._wire_read += len(chunk)
            self._progress.update(self._wire_read)
        return bytes(chunks)

    def _fill_encrypted(self, size: int) -> None:
        while len(self._plain_buffer) < size:
            if self._wire_read >= self._wire_size:
                raise EOFError("加密文件夹数据不完整")
            length_bytes = _recv_exact(self._conn, 4)
            if length_bytes is None:
                raise EOFError("加密文件夹数据不完整")
            ciphertext_size = struct.unpack("!I", length_bytes)[0]
            if (not 16 <= ciphertext_size <= CHUNK_SIZE + 16
                    or self._wire_read + 4 + ciphertext_size > self._wire_size):
                raise ValueError("加密文件夹分块非法")
            ciphertext = _recv_exact(self._conn, ciphertext_size)
            if ciphertext is None:
                raise EOFError("加密文件夹数据不完整")
            plain = self._decryptor.decrypt_chunk(ciphertext)
            if plain is None:
                raise ValueError("文件夹解密失败（两端口令不一致？）")
            self._wire_read += 4 + ciphertext_size
            self._plain_buffer.extend(plain)
            self._progress.update(self._wire_read)

    def read_exact(self, size: int) -> bytes:
        if size < 0 or self._plain_read + size > self._plain_size:
            raise ValueError("文件夹声明大小不一致")
        if self._encrypted:
            self._fill_encrypted(size)
            result = bytes(self._plain_buffer[:size])
            del self._plain_buffer[:size]
        else:
            result = self._read_clear(size)
        self._plain_read += size
        return result

    def copy_exact(self, output, size: int) -> None:
        remaining = size
        while remaining:
            chunk = self.read_exact(min(_BUFFER, remaining))
            output.write(chunk)
            remaining -= len(chunk)

    def finish(self) -> None:
        if (self._plain_read != self._plain_size or self._wire_read != self._wire_size
                or self._plain_buffer):
            raise ValueError("文件夹实际大小与声明不一致")


def _apply_mtime(path: str, mtime_ms: int) -> None:
    if mtime_ms <= 0:
        return
    try:
        seconds = mtime_ms / 1000.0
        try:
            os.utime(path, (seconds, seconds), follow_symlinks=False)
        except NotImplementedError:
            os.utime(path, (seconds, seconds))
    except (OSError, OverflowError, ValueError, NotImplementedError):
        pass


def _receive_folder_stream(reader: _FolderWireReader, staging: str) -> None:
    if reader.read_exact(4) != _FOLDER_MAGIC:
        raise ValueError("文件夹流标识非法")
    entry_count = struct.unpack("!I", reader.read_exact(4))[0]
    if entry_count > _MAX_FOLDER_ENTRIES:
        raise ValueError("文件夹条目过多")

    seen: set[str] = set()
    file_keys: set[str] = set()
    ancestor_keys: set[str] = set()
    directory_mtimes: list[tuple[str, int, int]] = []
    staging_abs = os.path.abspath(staging)
    for _index in range(entry_count):
        entry_type, path_size, file_size, mtime_ms = _FOLDER_ENTRY.unpack(
            reader.read_exact(_FOLDER_ENTRY.size))
        if not 0 < path_size <= _MAX_FOLDER_PATH:
            raise ValueError("文件夹路径长度非法")
        try:
            relative = reader.read_exact(path_size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("文件夹路径编码非法") from exc
        parts, collision_key = _portable_path_parts(relative)
        if collision_key in seen:
            raise ValueError(f"文件夹内存在重名路径：{relative}")

        normalized_parts = collision_key.split("/")
        parent_keys = ["/".join(normalized_parts[:i]) for i in range(1, len(parts))]
        if any(parent in file_keys for parent in parent_keys):
            raise ValueError(f"文件与目录结构冲突：{relative}")
        if entry_type == 1 and collision_key in ancestor_keys:
            raise ValueError(f"文件与目录结构冲突：{relative}")
        if entry_type not in (0, 1) or (entry_type == 0 and file_size != 0):
            raise ValueError("文件夹条目类型非法")
        if file_size > _MAX_FILE_SIZE:
            raise ValueError("文件夹内文件大小非法")

        target = os.path.abspath(os.path.join(staging_abs, *parts))
        try:
            if os.path.commonpath((staging_abs, target)) != staging_abs:
                raise ValueError("文件夹路径越界")
        except ValueError as exc:
            raise ValueError("文件夹路径越界") from exc

        seen.add(collision_key)
        ancestor_keys.update(parent_keys)
        if entry_type == 0:
            os.makedirs(target, exist_ok=True)
            directory_mtimes.append((target, mtime_ms, len(parts)))
        else:
            file_keys.add(collision_key)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as output:
                reader.copy_exact(output, file_size)
            _apply_mtime(target, mtime_ms)

    reader.finish()
    for directory, mtime_ms, _depth in sorted(
            directory_mtimes, key=lambda item: item[2], reverse=True):
        _apply_mtime(directory, mtime_ms)


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
                 on_progress: Callable[[str, str, int, int], None] | None = None,
                 on_transfer_end: Callable[[str, str, bool], None] | None = None):
        self.cfg = cfg
        self.on_sent = on_sent
        self.on_received = on_received
        self.on_status = on_status
        self.on_peers_changed = on_peers_changed
        self.on_progress = on_progress   # (kind:"send"/"recv", 文件名, 已传字节, 总字节)
        self.on_transfer_end = on_transfer_end  # (kind, 文件名, 是否完整完成)

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
        # mDNS 生命周期锁：_rebuild_mdns(网络监控线程)与 stop()(主/切换线程)
        # 可能并发执行；不加锁时 stop 刚拆完、rebuild 又建一个新 Zeroconf——
        # 节点已停止却还在局域网宣告，变成幽灵设备且实例泄漏。
        self._mdns_lock = threading.Lock()
        # 网络监控：待机唤醒/换网后 mDNS 组播 socket 会失效，需重建(见 _net_monitor_loop)
        self._last_local_ips: list[str] = []   # 上次建 mDNS 时的本机 IP，用于检测变化
        self._net_monitor_thread: threading.Thread | None = None

        # TCP 服务器
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._send_state_lock = threading.Lock()
        self._active_send_sock: socket.socket | None = None
        self._send_active = False
        self._send_cancelled = threading.Event()

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
        try:
            self._start_tcp_server()
        except OSError:
            self._running = False
            if self._server_sock:
                try:
                    self._server_sock.close()
                except OSError:
                    pass
                self._server_sock = None
            self._actual_port = 0
            self._status("墨洞未开启：监听端口启动失败")
            return

        # 1.5 注册手动添加的设备(乐观加入;真离线的话探测线程 ~10s 内剔除,
        #     回线后探测线程又会自动加回来——见 _probe_loop 末尾的兜底段)
        for entry in list(self.cfg.manual_peers or []):
            self._register_manual(entry)

        # 2. 对端存活探测线程(mDNS goodbye 丢包/对端崩溃的兜底，见 _probe_loop)
        self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
        self._probe_thread.start()

        # 3. 注册 mDNS 服务(测试模式跳过：只测 TCP 层，不受局域网环境干扰)
        if not self.cfg.enable_mdns:
            self._report_started()
            return

        with self._mdns_lock:
            self._setup_mdns()

        # 4. 网络监控线程：待机唤醒/换网后重建 mDNS(见 _net_monitor_loop)
        self._net_monitor_thread = threading.Thread(target=self._net_monitor_loop, daemon=True)
        self._net_monitor_thread.start()

        self._report_started()

    def _report_started(self) -> None:
        if self.cfg.listen_port and self._actual_port != self.cfg.listen_port:
            self._status(
                f"墨洞已开启 · 端口 {self.cfg.listen_port} 被占用，当前端口 {self._actual_port}")
        else:
            self._status(f"墨洞已开启 · {self.cfg.peer_name}")

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
                b"caps": _FOLDER_KIND.encode("ascii"),
                # 全部本机 IPv4:Android NSD 只解析出一个地址,而本机发出
                # 连接的源 IP 可能是另一块网卡(VPN/TUN/多网卡)——对端的
                # "仅接收目标设备"需要完整列表才能正确放行
                b"ips": ",".join(local_ips).encode("ascii"),
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
        self.cancel_send()
        with self._mdns_lock:
            self._teardown_mdns()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None

    def cancel_send(self) -> bool:
        """Cancel only the active outbound transfer; inbound transfers keep running."""
        self._send_cancelled.set()
        with self._send_state_lock:
            active = self._send_active
            sock = self._active_send_sock
        if sock is not None:
            try:
                # 取消要立刻生效:SO_LINGER(0) 让 close 直接 RST 丢弃发送缓冲
                # 里已排队的数据(最多 4MB)。否则内核会把缓冲慢慢发完才断开,
                # 跨网中继链路上对端还要"收"十几秒才看到传输中断。
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        return active

    # ---------- TCP 服务器 ----------
    def _start_tcp_server(self) -> None:
        """绑定 TCP 监听端口。listen_port=0 时由操作系统自动分配可用端口。"""
        def make_socket() -> socket.socket:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 监听 socket 的缓冲会被 accept 出的连接继承,并决定握手时协商的
            # 窗口缩放——大文件接收吞吐的天花板在这里定下
            _tune_transfer_socket(sock)
            return sock

        self._server_sock = make_socket()

        port = self.cfg.listen_port
        # 统一交给 OS 分配(port=0)或绑定指定端口。不用手动扫端口范围——
        # Windows 上 SO_REUSEADDR 允许多 socket 绑同一端口,手动扫描会给两个节点
        # 分到同一个端口,导致只有一方能收到连接。
        try:
            self._server_sock.bind(("", port))
        except OSError:
            if not port:
                raise
            self._server_sock.close()
            self._server_sock = make_socket()
            self._server_sock.bind(("", 0))

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
        """Receive one WHPP file or one atomic WHF1 folder transaction."""
        part_path = None
        folder_part_path = None
        want_ack = False
        ok = False
        transfer_name = ""
        transfer_started = False
        try:
            conn.settimeout(_RECV_IDLE_TIMEOUT)
            # Probe connections that close before sending four bytes remain silent.
            magic = _recv_exact(conn, 4)
            if magic == _CAP_MAGIC:
                body = json.dumps(
                    {"version": 1, "caps": [_FOLDER_KIND]},
                    separators=(",", ":"),
                ).encode("utf-8")
                conn.sendall(_CAP_MAGIC + struct.pack("!I", len(body)) + body)
                return
            if magic != _MAGIC:
                return

            # 仅接收目标设备：来源 IP 不是当前选中设备的地址就拒收
            if self.cfg.trusted_only:
                with self._lock:
                    sel = self._peers.get(self._selected_peer) if self._selected_peer else None
                    allowed = set(sel.hosts) if sel else set()
                if addr[0] not in allowed:
                    self._status(f"已拒收 {addr[0]} 的传输（仅接收目标设备）")
                    try:
                        conn.sendall(_ACK_FAIL)
                    except OSError:
                        pass
                    _drain(conn, _DRAIN_CAP)
                    return

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

            filename = _safe_filename(str(header.get("filename", "")))
            size = header.get("size", 0)
            encrypted = bool(header.get("encrypted", False))
            enc_mode = str(header.get("enc_mode", ""))
            want_ack = bool(header.get("want_ack", False))
            kind = str(header.get("kind", "file"))
            is_folder = kind == _FOLDER_KIND
            if kind not in ("", "file", _FOLDER_KIND):
                self._status(f"拒收 {filename}：不支持的传输类型")
                return

            size_limit = chunked_wire_size(_MAX_FILE_SIZE) if is_folder else _MAX_FILE_SIZE
            if (isinstance(size, bool) or not isinstance(size, int)
                    or not 0 <= size <= size_limit):
                self._status(f"拒收 {filename}：文件大小非法")
                return

            plain_size = size
            modified_ms = 0
            if is_folder:
                plain_size = header.get("plain_size", -1)
                modified_ms = header.get("mtime_ms", 0)
                if (isinstance(plain_size, bool) or not isinstance(plain_size, int)
                        or not 8 <= plain_size <= _MAX_FILE_SIZE):
                    self._status(f"拒收 {filename}：文件夹大小非法")
                    return
                if (isinstance(modified_ms, bool) or not isinstance(modified_ms, int)
                        or not 0 <= modified_ms <= 0xFFFFFFFFFFFFFFFF):
                    self._status(f"拒收 {filename}：文件夹时间非法")
                    return
                if encrypted:
                    if enc_mode != "chunked" or size != chunked_wire_size(plain_size):
                        self._status(f"拒收 {filename}：文件夹加密声明非法")
                        return
                elif size != plain_size:
                    self._status(f"拒收 {filename}：文件夹大小声明不一致")
                    return

            storage_size = plain_size if is_folder else size
            try:
                if storage_size + _DISK_MARGIN > shutil.disk_usage(self.cfg.inbox).free:
                    self._status(f"拒收 {filename}：磁盘空间不足")
                    _drain(conn, min(size, _DRAIN_CAP))
                    return
            except OSError:
                pass
            if encrypted and not self.cfg.secret:
                self._status(f"拒收 {filename}：对方启用了加密，本机未设口令")
                _drain(conn, min(size, _DRAIN_CAP))
                return
            if encrypted and enc_mode == "chunked" and size < 32:
                self._status(f"拒收 {filename}：加密流大小非法")
                return
            if encrypted and enc_mode != "chunked" and size > _MAX_WHE1_SIZE:
                self._status(f"拒收 {filename}：整块加密文件过大")
                _drain(conn, _DRAIN_CAP)
                return

            progress = _Progress(self.on_progress, "recv", filename, size)
            transfer_name = filename
            transfer_started = True

            if is_folder:
                folder_part_path = os.path.join(
                    self.cfg.inbox, f".inkhole-{uuid.uuid4().hex}.folder.part")
                os.mkdir(folder_part_path)
                reader = _FolderWireReader(
                    conn, size, plain_size, self.cfg.secret, encrypted, progress)
                _receive_folder_stream(reader, folder_part_path)
                _apply_mtime(folder_part_path, modified_ms)
                with self._lock:
                    dst = _unique_directory_path(self.cfg.inbox, filename)
                    os.replace(folder_part_path, dst)
                folder_part_path = None
            else:
                dst = _unique_path(self.cfg.inbox, filename)
                part_path = dst + f".{uuid.uuid4().hex[:8]}.part"
                if encrypted and enc_mode == "chunked":
                    hdr32 = _recv_exact(conn, 32)
                    if hdr32 is None:
                        raise EOFError("加密流头不完整")
                    decryptor = ChunkedDecryptor(self.cfg.secret, hdr32)
                    consumed = 32
                    progress.update(consumed)
                    with open(part_path, "wb") as output:
                        while consumed < size:
                            len_bytes = _recv_exact(conn, 4)
                            if len_bytes is None:
                                raise EOFError("加密文件数据不完整")
                            ct_len = struct.unpack("!I", len_bytes)[0]
                            if (not 16 <= ct_len <= CHUNK_SIZE + 16
                                    or consumed + 4 + ct_len > size):
                                raise ValueError("加密文件分块非法")
                            ciphertext = _recv_exact(conn, ct_len)
                            if ciphertext is None:
                                raise EOFError("加密文件数据不完整")
                            plain = decryptor.decrypt_chunk(ciphertext)
                            if plain is None:
                                raise ValueError("解密失败（两端口令不一致？）")
                            output.write(plain)
                            consumed += 4 + ct_len
                            progress.update(consumed)
                    if consumed != size:
                        raise EOFError("加密文件数据不完整")
                else:
                    remaining = size
                    with open(part_path, "wb") as output:
                        while remaining > 0:
                            chunk = conn.recv(min(_BUFFER, remaining))
                            if not chunk:
                                raise EOFError("文件数据不完整")
                            output.write(chunk)
                            remaining -= len(chunk)
                            progress.update(size - remaining)
                    if encrypted:
                        with open(part_path, "rb") as source:
                            plain = decrypt(self.cfg.secret, source.read())
                        if plain is None:
                            raise ValueError("解密失败（两端口令不一致？）")
                        with open(part_path, "wb") as output:
                            output.write(plain)

                with self._lock:
                    dst = _unique_path(self.cfg.inbox, filename)
                    os.replace(part_path, dst)
                part_path = None

            ok = True
            if self.on_received:
                self.on_received(dst)
            self._status(f"已接收：{os.path.basename(dst)}")
        except (EOFError, ConnectionResetError, ConnectionAbortedError):
            self._status(f"接收中断：{transfer_name or '未知文件'}")
        except Exception as e:
            self._status("接收失败", str(e))
        finally:
            if part_path and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            if folder_part_path:
                shutil.rmtree(folder_part_path, ignore_errors=True)
            if want_ack:
                try:
                    conn.sendall(_ACK_OK if ok else _ACK_FAIL)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
            if transfer_started and self.on_transfer_end:
                try:
                    self.on_transfer_end("recv", transfer_name, ok)
                except Exception:
                    pass

    # ---------- 发送文件 / 文件夹 ----------
    def _selected_send_peer(self) -> tuple[str | None, PeerInfo | None]:
        with self._lock:
            selected = self._selected_peer
            return selected, self._peers.get(selected) if selected else None

    def _probe_peer_capabilities(self, peer: PeerInfo) -> set[str]:
        """Actively probe capabilities so manual/Tailscale peers work without TXT."""
        hosts = sorted(peer.hosts or [peer.host],
                       key=lambda host: 1 if _is_cgnat_ip(host) else 0)
        for host in hosts:
            sock = None
            try:
                sock = _connect_transfer_socket(host, peer.port, _CONNECT_TIMEOUT)
                sock.settimeout(_CAP_TIMEOUT)
                sock.sendall(_CAP_MAGIC)
                if _recv_exact(sock, 4) != _CAP_MAGIC:
                    return set()
                size_bytes = _recv_exact(sock, 4)
                if size_bytes is None:
                    return set()
                body_size = struct.unpack("!I", size_bytes)[0]
                if not 0 < body_size <= _MAX_HEADER:
                    return set()
                body = _recv_exact(sock, body_size)
                if body is None:
                    return set()
                decoded = json.loads(body.decode("utf-8"))
                caps = decoded.get("caps", [])
                return {str(cap) for cap in caps} if isinstance(caps, list) else set()
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                continue
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        return set()

    def send_path(self, local_path: str,
                  should_cancel: Callable[[], bool] | None = None) -> bool:
        """Send a file, or stream a directory when the peer supports folder-v1."""
        if os.path.isfile(local_path):
            return self.send_file(local_path, should_cancel=should_cancel)
        if not os.path.isdir(local_path):
            self._status("文件不存在")
            return False

        selected, peer = self._selected_send_peer()
        if not peer:
            self._status("目标设备已离线" if selected else "请先选择目标设备")
            return False
        if _FOLDER_KIND in self._probe_peer_capabilities(peer):
            return self._send_folder_stream(local_path, peer, should_cancel)

        # Old clients only understand a single WHPP file. Build the legacy ZIP in
        # the queue worker (never the GUI thread), then clean it on every outcome.
        self._status("对端版本较旧，正在兼容打包文件夹…")
        zip_path = None
        try:
            zip_path = _zip_dir(local_path, should_cancel=should_cancel)
            return self.send_file(zip_path, should_cancel=should_cancel)
        except _SendCancelled:
            self._status(f"已取消发送：{os.path.basename(local_path)}")
            return False
        except Exception as exc:
            self._status("文件夹打包失败", str(exc))
            return False
        finally:
            if zip_path:
                shutil.rmtree(os.path.dirname(zip_path), ignore_errors=True)

    def _send_folder_stream(self, local_path: str, peer: PeerInfo,
                            should_cancel: Callable[[], bool] | None) -> bool:
        name = os.path.basename(os.path.abspath(local_path)) or "folder"
        completed = False

        def cancellation_requested() -> bool:
            if self._send_cancelled.is_set():
                return True
            if should_cancel is None:
                return False
            try:
                return bool(should_cancel())
            except Exception:
                return False

        self._send_cancelled.clear()
        with self._send_state_lock:
            self._send_active = True
            self._active_send_sock = None
        try:
            _portable_path_parts(name)
            self._status(f"正在扫描文件夹：{name}")
            manifest = _scan_folder(local_path, cancellation_requested)
            if cancellation_requested():
                raise _SendCancelled()
            encrypted = bool(self.cfg.secret)
            wire_size = (chunked_wire_size(manifest.plain_size)
                         if encrypted else manifest.plain_size)
            header_dict = {
                "filename": name,
                "size": wire_size,
                "plain_size": manifest.plain_size,
                "kind": _FOLDER_KIND,
                "mtime_ms": manifest.root_mtime_ms,
                "encrypted": encrypted,
                "want_ack": True,
            }
            if encrypted:
                header_dict["enc_mode"] = "chunked"
            header = json.dumps(header_dict, separators=(",", ":")).encode("utf-8")
            progress = _Progress(self.on_progress, "send", name, wire_size)

            sock = None
            last_err: OSError | None = None
            send_hosts = sorted(peer.hosts or [peer.host],
                                key=lambda host: 1 if _is_cgnat_ip(host) else 0)
            for host in send_hosts:
                try:
                    sock = _connect_transfer_socket(host, peer.port, _CONNECT_TIMEOUT)
                    break
                except OSError as exc:
                    last_err = exc
            if sock is None:
                raise last_err if last_err else OSError("无可用地址")

            try:
                with self._send_state_lock:
                    self._active_send_sock = sock
                if cancellation_requested():
                    raise _SendCancelled()
                sock.settimeout(_SEND_IO_TIMEOUT)
                sock.sendall(_MAGIC)
                sock.sendall(struct.pack("!I", len(header)))
                sock.sendall(header)

                sent = 0
                with _FolderPayloadReader(manifest) as source:
                    if encrypted:
                        for blob in encrypt_chunks(self.cfg.secret, source):
                            if cancellation_requested():
                                raise _SendCancelled()
                            sock.sendall(blob)
                            sent += len(blob)
                            progress.update(sent)
                    else:
                        while sent < manifest.plain_size:
                            if cancellation_requested():
                                raise _SendCancelled()
                            chunk = source.read(min(_BUFFER, manifest.plain_size - sent))
                            if not chunk:
                                raise OSError("文件夹读取不完整")
                            sock.sendall(chunk)
                            sent += len(chunk)
                            progress.update(sent)
                if sent != wire_size:
                    raise OSError("文件夹发送大小不一致")

                sock.settimeout(60)
                try:
                    response = sock.recv(1)
                except socket.timeout:
                    # Android may still be publishing a very large folder to
                    # MediaStore after the private atomic commit.
                    if cancellation_requested():
                        raise _SendCancelled()
                    response = _ACK_OK
                except OSError:
                    if cancellation_requested():
                        raise _SendCancelled()
                    response = b""
                # folder-v1 was capability-negotiated, so EOF or reset cannot
                # be treated as the legacy client's implicit success signal.
                if response != _ACK_OK:
                    self._status(f"{peer.name} 接收失败（口令不一致、路径或存储问题）")
                    return False
            finally:
                with self._send_state_lock:
                    if self._active_send_sock is sock:
                        self._active_send_sock = None
                try:
                    sock.close()
                except OSError:
                    pass

            completed = True
            if self.on_sent:
                self.on_sent(name)
            self._status(f"已发送：{name}")
            return True
        except _SendCancelled:
            self._status(f"已取消发送：{name}")
            return False
        except (ConnectionRefusedError, socket.timeout, OSError, ValueError) as exc:
            if cancellation_requested():
                self._status(f"已取消发送：{name}")
            else:
                self._status("发送失败", str(exc))
            return False
        except Exception as exc:
            if cancellation_requested():
                self._status(f"已取消发送：{name}")
            else:
                self._status("发送失败", str(exc))
            return False
        finally:
            with self._send_state_lock:
                self._active_send_sock = None
                self._send_active = False
            self._send_cancelled.clear()
            if self.on_transfer_end:
                try:
                    self.on_transfer_end("send", name, completed)
                except Exception:
                    pass

    def send_file(self, local_path: str,
                  should_cancel: Callable[[], bool] | None = None) -> bool:
        """把文件直接发给选中的对端。成功返回 True。

        header 带 want_ack：新版接收方处理完回 1 字节回执，落盘失败能被
        发送方感知；老版接收方(v1.0.0)读完数据直接关连接，按成功处理。
        """
        if not os.path.isfile(local_path):
            self._status("文件不存在")
            return False

        with self._lock:
            selected = self._selected_peer
            peer = self._peers.get(selected) if selected else None
        if not peer:
            self._status("目标设备已离线" if selected else "请先选择目标设备")
            return False

        name = os.path.basename(local_path)
        completed = False

        def cancellation_requested() -> bool:
            if self._send_cancelled.is_set():
                return True
            if should_cancel is None:
                return False
            try:
                return bool(should_cancel())
            except Exception:
                return False

        self._send_cancelled.clear()
        with self._send_state_lock:
            self._send_active = True
            self._active_send_sock = None
        try:
            if cancellation_requested():
                raise _SendCancelled()
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
                if cancellation_requested():
                    raise _SendCancelled()
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

            # 多网卡/VPN 场景：逐个地址尝试，先通先用。局域网/直连地址优先,
            # Tailscale(100.x)殿后——同 WiFi 时若先连 100.x 会绕道 relay,
            # 把直连速度拖成几百 KB/s
            sock = None
            last_err: OSError | None = None
            send_hosts = sorted(peer.hosts or [peer.host],
                                key=lambda h: 1 if _is_cgnat_ip(h) else 0)
            for host in send_hosts:
                try:
                    sock = _connect_transfer_socket(host, peer.port, _CONNECT_TIMEOUT)
                    break
                except OSError as e:
                    last_err = e
            if sock is None:
                raise last_err if last_err else OSError("无可用地址")

            try:
                with self._send_state_lock:
                    self._active_send_sock = sock
                if cancellation_requested():
                    raise _SendCancelled()
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
                            if cancellation_requested():
                                raise _SendCancelled()
                            sock.sendall(blob)
                            sent += len(blob)
                            progress.update(sent)
                elif data is not None:
                    # 加密数据已在内存，分块发送
                    while sent < len(data):
                        if cancellation_requested():
                            raise _SendCancelled()
                        sock.sendall(data[sent:sent + _BUFFER])
                        sent = min(sent + _BUFFER, len(data))
                        progress.update(sent)
                else:
                    # 明文：流式从磁盘读
                    with open(local_path, "rb") as f:
                        while True:
                            if cancellation_requested():
                                raise _SendCancelled()
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
                    if cancellation_requested():
                        raise _SendCancelled()
                    resp = b""
                if resp == _ACK_FAIL:
                    self._status(f"{peer.name} 接收失败（口令不一致、被拒收或存储问题）")
                    return False
            finally:
                with self._send_state_lock:
                    if self._active_send_sock is sock:
                        self._active_send_sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            completed = True
            if self.on_sent:
                self.on_sent(name)
            self._status(f"已发送：{name}")
            return True
        except _SendCancelled:
            self._status(f"已取消发送：{name}")
            return False
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            if cancellation_requested():
                self._status(f"已取消发送：{name}")
            else:
                self._status("发送失败", str(e))
            return False
        except Exception as e:
            if cancellation_requested():
                self._status(f"已取消发送：{name}")
            else:
                self._status("发送失败", str(e))
            return False
        finally:
            with self._send_state_lock:
                self._active_send_sock = None
                self._send_active = False
            self._send_cancelled.clear()
            if self.on_transfer_end:
                try:
                    self.on_transfer_end("send", name, completed)
                except Exception:
                    pass

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
        self._status(f"目标: {name}" if name else "未选择目标")

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
            self._status(f"发现: {display_name}")

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

    # ---------- 手动设备(Tailscale/固定 IP 直连) ----------
    @staticmethod
    def _manual_key(entry: dict) -> str:
        """手动设备的稳定标识,顶替 mDNS service_name 参与增删/探测去重。"""
        return f"manual|{entry['host']}|{int(entry['port'])}"

    def _register_manual(self, entry: dict) -> None:
        self._on_peer_added(str(entry.get("name") or entry["host"]),
                            str(entry["host"]), int(entry["port"]),
                            service_name=self._manual_key(entry))

    def add_manual_peer(self, name: str, host: str, port: int) -> None:
        """新增(或更新)一台手动设备并立即注册。调用方负责持久化配置。"""
        port = int(port)
        entry = {"name": name, "host": host, "port": port}
        self.cfg.manual_peers = [
            m for m in (self.cfg.manual_peers or [])
            if not (m["host"] == host and int(m["port"]) == port)]
        self.cfg.manual_peers.append(entry)
        if self._running:
            self._register_manual(entry)

    def remove_manual_peer(self, host: str, port: int) -> None:
        """删除手动设备:同时移出配置与在线列表。调用方负责持久化配置。"""
        port = int(port)
        self.cfg.manual_peers = [
            m for m in (self.cfg.manual_peers or [])
            if not (m["host"] == host and int(m["port"]) == port)]
        key = f"manual|{host}|{port}"
        display = None
        with self._lock:
            for n, p in self._peers.items():
                if p.service_name == key:
                    display = n
                    break
        if display is not None:
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
            auto_removed = False
            ts_ip = _cgnat_source_ip()
            for key, hosts, port, display in targets:
                seen.add(key)
                is_manual = key.startswith("manual|")
                # 本机 Tailscale 不在线:纯 CGNAT 地址的手动设备确定不可达,
                # 立即剔除——确定性判断不烧 4 轮抖动容忍,关 Tailscale 后
                # 跨网设备 5 秒内消失而不是挂半分多钟
                if is_manual and ts_ip is None and hosts \
                        and all(_is_cgnat_ip(h) for h in hosts):
                    strikes.pop(key, None)
                    self._on_peer_removed(display)
                    continue
                # 手动设备(Tailscale)探活给双倍超时:空闲后懒惰唤醒(打洞/DERP
                # 建链)首次握手常超默认超时,太紧会把在线的跨网设备判死
                probe_timeout = (self._probe_timeout * 2
                                 if is_manual else self._probe_timeout)
                alive = False
                for host in hosts:
                    try:
                        _probe_connect(host, port, probe_timeout)
                        alive = True
                        break
                    except OSError:
                        continue
                if alive:
                    strikes.pop(key, None)
                    continue
                # 手动设备用双倍容忍:手机息屏时厂商省电会让 WiFi 短暂休眠,
                # 探活偶发失败;阈值太急会造成"息屏就消失、亮屏又回来"的抖动
                threshold = (self._probe_strikes * 2
                             if key.startswith("manual|") else self._probe_strikes)
                if strikes.get(key, 0) + 1 >= threshold:
                    strikes.pop(key, None)
                    self._on_peer_removed(display)
                    if not key.startswith("manual|"):
                        auto_removed = True
                else:
                    strikes[key] = strikes.get(key, 0) + 1
            # 已经不在对端表里的条目不再计数(正常离线/被 mDNS 先移除)
            for k in list(strikes):
                if k not in seen:
                    del strikes[k]

            if auto_removed and self.cfg.enable_mdns:
                threading.Thread(
                    target=lambda: self._rebuild_mdns("设备离线", announce=False),
                    daemon=True).start()

            # 手动设备兜底:被判离线后不会有 mDNS 通告拉它回来,
            # 每轮探测不在表里的手动设备,连得上就重新加入。
            with self._lock:
                known = {p.service_name for p in self._peers.values()}
            for entry in list(self.cfg.manual_peers or []):
                if not self._running:
                    break
                key = self._manual_key(entry)
                if key in known:
                    continue
                try:
                    _probe_connect(entry["host"], int(entry["port"]),
                                   self._probe_timeout * 2)
                except OSError:
                    continue
                self._register_manual(entry)

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

    def _rebuild_mdns(self, reason: str, announce: bool = True) -> None:
        """拆掉并重新创建整个 mDNS 层(唤醒/换网后自愈)。

        对端表保留：浏览器重建后会重新收到在线设备的通告刷新地址，
        探测线程会清理掉真正够不着的旧设备，不必在这里清空(清空会让
        菜单瞬间空掉、还丢掉当前选中)。

        与 stop() 竞争 _mdns_lock；拿到锁后必须复查 _running——
        stop 可能刚在锁内拆完，这里再重建就泄漏了。
        """
        if not self._running or not self.cfg.enable_mdns:
            return
        if announce:
            self._status(f"检测到{reason}，正在重建设备发现…")
        with self._mdns_lock:
            if not self._running:
                return   # stop() 抢先完成:保持拆除状态,绝不再注册
            try:
                self._teardown_mdns()
            except Exception:
                pass
            try:
                self._setup_mdns()
            except Exception as e:
                self._status("设备发现重建失败", str(e))
                return
        if announce:
            self._status("设备发现已重建")

    # ---------- 工具 ----------
    def _status(self, msg: str, detail: str = "") -> None:
        if detail:
            print(f"[P2P] {msg} | {detail}", flush=True)
        if self.on_status:
            self.on_status(f"{msg}: {detail}" if detail else msg)

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


def _unique_directory_path(directory: str, name: str) -> str:
    """Return `<name>`, `<name> (2)`, ... without treating dots as extensions."""
    dst = os.path.join(directory, name)
    if not os.path.exists(dst):
        return dst
    n = 2
    while True:
        dst = os.path.join(directory, f"{name} ({n})")
        if not os.path.exists(dst):
            return dst
        n += 1


def _zip_dir(src_dir: str,
             should_cancel: Callable[[], bool] | None = None) -> str:
    """把目录递归打包成一个临时 zip，返回临时 zip 绝对路径。

    zip 名为 <目录basename>.zip，落在独立临时目录里（调用方发送后应删除
    整个临时目录）。arcname 用相对 src_dir 的路径，保持目录结构；空目录
    也生成合法（空）zip。用 ZIP_DEFLATED 压缩。
    """
    import zipfile
    src_dir = os.path.abspath(src_dir)
    base = os.path.basename(src_dir.rstrip("/\\")) or "folder"
    _portable_path_parts(base)
    manifest = _scan_folder(src_dir, should_cancel)
    tmp_root = tempfile.mkdtemp(prefix="inkhole_zip_")
    zip_path = os.path.join(tmp_root, base + ".zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for entry in manifest.entries:
                if should_cancel and should_cancel():
                    raise _SendCancelled()
                if entry.is_dir:
                    z.writestr(zipfile.ZipInfo(entry.path + "/"), "")
                    continue
                with open(entry.source, "rb") as source:
                    with z.open(entry.path, "w", force_zip64=True) as output:
                        remaining = entry.size
                        while remaining:
                            if should_cancel and should_cancel():
                                raise _SendCancelled()
                            chunk = source.read(min(_BUFFER, remaining))
                            if not chunk:
                                raise OSError(f"打包时文件发生变化：{entry.path}")
                            output.write(chunk)
                            remaining -= len(chunk)
                        if source.read(1):
                            raise OSError(f"打包时文件大小发生变化：{entry.path}")
        return zip_path
    except Exception:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise


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
        on_sent=lambda n: print(f"[发送] {n}"),
        on_received=lambda p: print(f"[接收] {p}"),
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
