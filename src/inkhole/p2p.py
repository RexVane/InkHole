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
  WHPC v3 独立连接使用随机挑战验证设备身份和能力。

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
import hmac
import hashlib
import ipaddress
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

from .crypto import (encrypt_chunks, chunked_wire_size, ChunkedDecryptor,
                     CHUNK_SIZE)
from .device_identity import (DeviceIdentity, capability_message,
                              public_fingerprint, receiver_message,
                              transfer_message, verify)

# ---------- 常量 ----------
_SERVICE_TYPE = "_inkhole._tcp.local."
_MAGIC = b"WHPP"          # InkHole P2P Protocol magic
_CAP_MAGIC = b"WHPC"      # capability probe; kept separate from file frames
_CORE_MAGIC = b"IKCI"     # authenticated loopback ingress from transport core
_CAP_VERSION = 3
_PROTOCOL_VERSION = 3
_FOLDER_MAGIC = b"WHF1"   # streamed folder payload magic
_FOLDER_KIND = "folder-v1"
_RELIABLE_KIND = "reliable-v3"
_CAPABILITIES = (_FOLDER_KIND, _RELIABLE_KIND)
_FOLDER_ENTRY = struct.Struct("!BIQQ")  # type, path bytes, file size, mtime ms
_BUFFER = 256 * 1024      # 256KB 传输块，降低大文件跨网传输的 Python IO 调用开销
_SOCKET_BUFFER = 4 * 1024 * 1024   # TCP 窗口上限:4MB @ RTT 200ms(DERP) ≈ 20MB/s,
                                   # @ RTT 60ms(WiFi 抖动) ≈ 66MB/s,高于链路真实能力
_MAX_HEADER = 64 * 1024            # header JSON 长度上限(来自网络，不可信)
_MAX_FILE_SIZE = 1 << 40           # 单文件 1TB 上限，防恶意 size 声明
_RECV_IDLE_TIMEOUT = 300           # 接收 socket 空闲超时(秒)，防半开连接永久占住线程
_SEND_IO_TIMEOUT = 60              # 发送数据阶段单次 IO 超时(秒)
_DISK_MARGIN = 256 * 1024 * 1024   # 收完文件后磁盘至少还要剩这么多才接收
_ACK_OK = b"\x01"                  # 接收方回执：成功落盘
_ACK_FAIL = b"\x00"                # 接收方回执：失败(中断/解密失败/写盘失败)
_RESUME = b"\x02"                  # 接收方回执：后跟 8B 已持久化明文偏移
_DIGEST_SIZE = 32
_MAX_IDENTITY_FIELD = 512
_TRANSFER_RETRIES = 3
_CHECKPOINT_MAX_AGE = 7 * 24 * 60 * 60
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

# 接收端的可选自动分类。类别名是配置文件中的稳定键，显示名称只用于
# 默认子目录和设置界面，便于以后调整文案而不破坏已有配置。
_INBOX_CATEGORY_MEDIA = "media"
_INBOX_CATEGORY_ARCHIVE = "archive"
_INBOX_CATEGORY_FILE = "file"
_INBOX_CATEGORY_FOLDER = "folder"
_INBOX_CATEGORIES = (
    _INBOX_CATEGORY_MEDIA,
    _INBOX_CATEGORY_ARCHIVE,
    _INBOX_CATEGORY_FILE,
    _INBOX_CATEGORY_FOLDER,
)
_INBOX_CATEGORY_LABELS = {
    _INBOX_CATEGORY_MEDIA: "图片和视频",
    _INBOX_CATEGORY_ARCHIVE: "压缩包",
    _INBOX_CATEGORY_FILE: "文件",
    _INBOX_CATEGORY_FOLDER: "文件夹",
}
_MEDIA_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic",
    "heif", "svg", "mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv",
    "flv", "mpeg", "mpg", "3gp", "ts",
}
_ARCHIVE_EXTENSIONS = {
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "tbz", "tbz2",
    "txz", "zst", "lz", "lz4", "cab", "iso", "dmg",
}
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_VIRTUAL_INTERFACE_KEYWORDS = (
    "vmware", "virtualbox", "vbox", "hyper-v", "vethernet",
    "docker", "vmmem", "wsl",
)


class _SendCancelled(Exception):
    pass


class _IdentityMismatch(OSError):
    pass


class _TailnetUnavailable(OSError):
    pass


class _ReceiverRejected(OSError):
    pass


@dataclass(frozen=True)
class _ResolvedEndpoint:
    family: int
    socktype: int
    proto: int
    sockaddr: tuple
    address: str

    @property
    def is_tailnet(self) -> bool:
        return _is_tailnet_ip(self.address)


@dataclass(frozen=True)
class _ProbeResult:
    instance_id: str
    peer_name: str
    capabilities: frozenset[str]
    connected_address: str
    public_key: str
    fingerprint: str


def _tune_transfer_socket(sock: socket.socket) -> None:
    """放大 TCP 收发缓冲。必须在 bind(服务端)/connect(客户端)之前调用：
    窗口缩放因子在握手时按当时的缓冲协商，连接建立后再放大只改本地队列、
    不改窗口上限；且显式设置会禁用系统自动调优，设晚了反而把窗口钉死。"""
    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, _SOCKET_BUFFER)
        except OSError:
            pass


def _plain_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a numeric address while tolerating an IPv6 scope suffix."""
    try:
        return ipaddress.ip_address(str(raw).split("%", 1)[0])
    except ValueError:
        return None


def _is_tailnet_ip(host: str) -> bool:
    address = _plain_ip(host)
    return bool(address and (address in _TAILNET_V4 or address in _TAILNET_V6))


def _is_cgnat_ip(host: str) -> bool:
    """Backward-compatible IPv4 helper retained for callers and tests."""
    address = _plain_ip(host)
    return bool(isinstance(address, ipaddress.IPv4Address)
                and address in _TAILNET_V4)


def _valid_instance_id(value: object) -> bool:
    text = str(value or "")
    return len(text) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in text)


def _resolve_endpoints(host: str, port: int) -> list[_ResolvedEndpoint]:
    endpoints: list[_ResolvedEndpoint] = []
    seen: set[tuple[int, str]] = set()
    for af, socktype, proto, _canon, sockaddr in socket.getaddrinfo(
            host, port, 0, socket.SOCK_STREAM):
        address = str(sockaddr[0]).split("%", 1)[0]
        key = (af, address)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append(_ResolvedEndpoint(
            af, socktype, proto, sockaddr, address))
    # A directly routed/LAN endpoint wins over a Tailnet path when both exist.
    return sorted(endpoints, key=lambda endpoint: endpoint.is_tailnet)


def _resolved_addresses(hosts: list[str]) -> list[str]:
    addresses: list[str] = []
    for host in hosts:
        try:
            endpoints = _resolve_endpoints(host, 0)
        except (socket.gaierror, OSError):
            continue
        for endpoint in endpoints:
            if endpoint.address not in addresses:
                addresses.append(endpoint.address)
    return addresses


def _tailnet_source_ip(family: int = socket.AF_INET) -> str | None:
    """Return a local Tailnet address matching the destination family."""
    try:
        import psutil
        interfaces = list(psutil.net_if_addrs().items())
        # Prefer an explicitly named Tailscale adapter when the platform exposes one.
        interfaces.sort(key=lambda item: 0 if "tailscale" in item[0].lower() else 1)
        for _iface, addrs in interfaces:
            for addr in addrs:
                if addr.family == family and _is_tailnet_ip(addr.address):
                    return str(addr.address).split("%", 1)[0]
    except ImportError:
        try:
            for *_ignored, sockaddr in socket.getaddrinfo(
                    socket.gethostname(), None, family):
                if _is_tailnet_ip(sockaddr[0]):
                    return str(sockaddr[0]).split("%", 1)[0]
        except (socket.gaierror, OSError):
            pass
    except OSError:
        pass
    return None


def _cgnat_source_ip() -> str | None:
    """Backward-compatible name for the local Tailnet IPv4 address."""
    return _tailnet_source_ip(socket.AF_INET)


def _probe_connect(host: str, port: int, timeout: float) -> None:
    """Compatibility wrapper: only a valid WHPC v3 response counts as alive."""
    _probe_peer(host, port, timeout)


def _connect_transfer_socket(host: str, port: int, timeout: float) -> socket.socket:
    """Resolve first, then bind Tailnet endpoints before connecting."""
    err: OSError | None = None
    try:
        endpoints = _resolve_endpoints(host, port)
    except socket.gaierror as exc:
        raise OSError(f"无法解析地址 {host}") from exc
    for endpoint in endpoints:
        sock = None
        try:
            sock = socket.socket(endpoint.family, endpoint.socktype, endpoint.proto)
            _tune_transfer_socket(sock)
            if endpoint.is_tailnet:
                src = _tailnet_source_ip(endpoint.family)
                if src is None:
                    raise _TailnetUnavailable("Tailscale 接口不在线")
                if endpoint.family == socket.AF_INET6:
                    sock.bind((src, 0, 0, 0))
                else:
                    sock.bind((src, 0))
            sock.settimeout(timeout)
            sock.connect(endpoint.sockaddr)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    raise err if err is not None else OSError(f"无法解析地址 {host}")


def _connect_peer_socket(peer: "PeerInfo", host: str, timeout: float) -> socket.socket:
    """Connect to a peer and authenticate loopback transport endpoints."""
    sock = _connect_transfer_socket(host, peer.port, timeout)
    if peer.endpoint_token:
        try:
            token = peer.endpoint_token.encode("ascii")
        except UnicodeEncodeError as exc:
            sock.close()
            raise OSError("跨网端点令牌无效") from exc
        try:
            sock.sendall(b"IKAT" + token)
        except OSError:
            sock.close()
            raise
    return sock


def _probe_peer(host: str, port: int, timeout: float,
                expected_instance_id: str = "",
                expected_fingerprint: str = "") -> _ProbeResult:
    sock = _connect_transfer_socket(host, port, timeout)
    try:
        sock.settimeout(timeout)
        nonce = os.urandom(32)
        sock.sendall(_CAP_MAGIC + nonce)
        if _recv_exact(sock, 4) != _CAP_MAGIC:
            raise OSError("目标不是新版墨洞设备")
        size_bytes = _recv_exact(sock, 4)
        if size_bytes is None:
            raise OSError("WHPC 响应不完整")
        body_size = struct.unpack("!I", size_bytes)[0]
        if not 0 < body_size <= _MAX_HEADER:
            raise OSError("WHPC 响应大小非法")
        body = _recv_exact(sock, body_size)
        if body is None:
            raise OSError("WHPC 响应不完整")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OSError("WHPC 响应格式非法") from exc
        if not isinstance(decoded, dict):
            raise OSError("WHPC 响应格式非法")
        instance_id = str(decoded.get("instance_id", "")).lower()
        peer_name = str(decoded.get("peer_name", "")).strip()
        public_key = str(decoded.get("public_key", ""))
        signature = str(decoded.get("signature", ""))
        caps = decoded.get("caps")
        if (decoded.get("version") != _CAP_VERSION
                or not _valid_instance_id(instance_id)
                or not peer_name
                or not isinstance(caps, list)
                or any(not isinstance(cap, str) for cap in caps)):
            raise OSError("目标不支持 WHPC v3")
        try:
            fingerprint = public_fingerprint(public_key)
        except ValueError as exc:
            raise OSError("设备公钥无效") from exc
        if not verify(public_key, capability_message(
                nonce, instance_id, peer_name, _CAP_VERSION, caps), signature):
            raise _IdentityMismatch("设备身份签名无效")
        if expected_instance_id and instance_id != expected_instance_id.lower():
            raise _IdentityMismatch("设备身份已变化，请删除后重新添加")
        if (expected_fingerprint
                and fingerprint != expected_fingerprint.lower()):
            raise _IdentityMismatch("设备密钥已变化，请撤销信任后重新配对")
        connected = str(sock.getpeername()[0]).split("%", 1)[0]
        return _ProbeResult(instance_id, peer_name, frozenset(caps), connected,
                            public_key, fingerprint)
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------- 配置 ----------
@dataclass
class P2PConfig:
    inbox: str = "received"        # 收件箱：收到的文件落在这里
    inbox_auto_classify: bool = False
    # 自动分类开启时各类别的目标目录；空值使用 inbox 下的默认中文子目录。
    inbox_category_dirs: dict = field(default_factory=dict)
    listen_port: int = 0           # TCP 监听端口；0 = 操作系统自动分配
    peer_name: str = ""            # 本机显示名；空则用 hostname
    secret: str = ""               # 保存的端到端加密口令(实际是否使用由 encryption_enabled 决定)
    enable_mdns: bool = True       # False = 只起 TCP 不碰 mDNS(测试用，手动注册对端)
    trusted_only: bool = False     # True = 只接受当前选中目标设备的连接，其余拒收
    instance_id: str = ""          # 32 位本机唯一实例 ID；服务名只使用 8 位短后缀
    # 手动添加的设备(Tailscale/固定 IP 直连用)：mDNS 组播不穿虚拟网卡,
    # 这些设备靠探测线程维持在线状态。可选 instance_id 为首次信任绑定。
    manual_peers: list = field(default_factory=list)
    # None = 按旧配置兼容:有口令即启用;显式 False 可保留口令但暂时停用加密
    encryption_enabled: bool | None = None
    # Runtime-only capability used by the local Go transport core. It is never
    # advertised or persisted and authenticates loopback-forwarded WHPP frames.
    core_ingress_token: str = ""
    # Production callers load this PKCS#8 key from the OS credential store.
    # Empty values generate an in-memory identity for tests and headless use.
    identity_private_key: str = ""
    # Public fingerprints are not secrets. They persist explicit device trust.
    trusted_peers: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.peer_name:
            self.peer_name = socket.gethostname()
        if not _valid_instance_id(self.instance_id):
            self.instance_id = uuid.uuid4().hex
        else:
            self.instance_id = self.instance_id.lower()
        self.encryption_enabled = (
            bool(self.secret) if self.encryption_enabled is None
            else bool(self.encryption_enabled))
        raw_category_dirs = (self.inbox_category_dirs
                             if isinstance(self.inbox_category_dirs, dict) else {})
        self.inbox_category_dirs = {
            category: str(raw_category_dirs.get(category) or "").strip()
            for category in _INBOX_CATEGORIES
        }
        self.trusted_peers = {
            str(instance).lower(): str(fingerprint).lower()
            for instance, fingerprint in dict(self.trusted_peers or {}).items()
            if (_valid_instance_id(instance)
                and _valid_sha256(str(fingerprint).lower()))
        }

    @property
    def active_secret(self) -> str:
        """Return the secret currently allowed to protect wire data."""
        return self.secret if self.encryption_enabled else ""


def inbox_category_for(filename: str, kind: str = "file") -> str:
    """Return the stable automatic-classification key for one top-level item."""
    if kind == _FOLDER_KIND:
        return _INBOX_CATEGORY_FOLDER
    extension = os.path.splitext(str(filename).lower())[1].lstrip(".")
    if extension in _MEDIA_EXTENSIONS:
        return _INBOX_CATEGORY_MEDIA
    if extension in _ARCHIVE_EXTENSIONS:
        return _INBOX_CATEGORY_ARCHIVE
    return _INBOX_CATEGORY_FILE


def inbox_root_for(cfg: P2PConfig, filename: str, kind: str = "file") -> str:
    """Resolve the receive root before any temporary data is written."""
    if not cfg.inbox_auto_classify:
        return cfg.inbox
    category = inbox_category_for(filename, kind)
    return _inbox_category_root(cfg, category)


def _inbox_category_root(cfg: P2PConfig, category: str) -> str:
    configured = cfg.inbox_category_dirs.get(category, "")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(cfg.inbox, _INBOX_CATEGORY_LABELS[category])


def inbox_roots(cfg: P2PConfig) -> list[str]:
    """Return all roots that may contain receive checkpoints."""
    roots = [cfg.inbox]
    if cfg.inbox_auto_classify:
        roots.extend(_inbox_category_root(cfg, category)
                     for category in _INBOX_CATEGORIES)
    result = []
    seen = set()
    for root in roots:
        normalized = os.path.abspath(os.path.expanduser(root))
        if normalized not in seen:
            result.append(root)
            seen.add(normalized)
    return result


class PeerInfo:
    """一个已发现的对端节点。

    name         显示名(菜单里看到的；重名设备会带 " (2)" 后缀)
    host         主地址(显示用，也是首选连接地址)
    hosts        全部已知地址(多网卡/VPN 场景逐个尝试连接)
    service_name mDNS 完整服务名(唯一，用于离线事件精确匹配；手动注册可为空)
    """
    __slots__ = ("name", "host", "port", "service_name", "hosts",
                 "instance_id", "capabilities", "manual", "transport",
                 "endpoint_token", "public_key", "identity_fingerprint")

    def __init__(self, name: str, host: str, port: int,
                 service_name: str = "", hosts: list[str] | None = None,
                 instance_id: str = "", capabilities: set[str] | frozenset[str] | None = None,
                 manual: bool = False, transport: str = "",
                 endpoint_token: str = "", public_key: str = "",
                 identity_fingerprint: str = ""):
        self.name = name
        self.host = host
        self.port = port
        self.service_name = service_name
        self.hosts = [h for h in (hosts or []) if h]
        if host and host not in self.hosts:
            self.hosts.insert(0, host)
        self.instance_id = instance_id.lower()
        self.capabilities = frozenset(capabilities or ())
        self.manual = bool(manual)
        self.transport = str(transport or ("tailscale" if manual else "lan"))
        self.endpoint_token = str(endpoint_token or "")
        self.public_key = str(public_key or "")
        self.identity_fingerprint = str(identity_fingerprint or "").lower()

    def __repr__(self):
        return f"PeerInfo({self.name!r}, {self.host}:{self.port})"

    def __str__(self):
        return f"{self.name} ({self.host})"

def _service_label(name: str, instance_id: str) -> str:
    """mDNS 服务实例标签：显示名 + 实例 ID 后缀，保证局域网内唯一。

    显示名里的 "." 会破坏服务名解析(DNS 标签分隔符)，替换掉；
    单个 DNS 标签最长 63 字节，utf-8 截断到 40 字节给后缀留余量。
    """
    label = name.replace(".", "-")
    raw = label.encode("utf-8")[:40]
    label = raw.decode("utf-8", errors="ignore")
    return f"{label}-{instance_id[:8]}"


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


class _ExactFileReader:
    """Expose a completed plaintext checkpoint to the WHF1 parser."""

    def __init__(self, path: str, size: int):
        self._source = open(path, "rb")
        self._size = size
        self._read = 0

    def read_exact(self, size: int) -> bytes:
        if size < 0 or self._read + size > self._size:
            raise EOFError("文件夹数据超出声明大小")
        data = self._source.read(size)
        if len(data) != size:
            raise EOFError("文件夹数据不完整")
        self._read += size
        return data

    def copy_exact(self, output, size: int) -> None:
        remaining = size
        while remaining:
            chunk = self.read_exact(min(_BUFFER, remaining))
            output.write(chunk)
            remaining -= len(chunk)

    def finish(self) -> None:
        if self._read != self._size or self._source.read(1):
            raise ValueError("文件夹数据大小不一致")

    def close(self) -> None:
        self._source.close()


class _ProgressReader:
    """Count plaintext reads so resumed and encrypted sends report one scale."""

    def __init__(self, source, progress, offset: int):
        self._source = source
        self._progress = progress
        self._done = offset

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        self._done += len(data)
        self._progress.update(self._done)
        return data


def _discard_exact(source, size: int) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(_BUFFER, remaining))
        if not chunk:
            raise OSError("续传源数据不完整")
        remaining -= len(chunk)


def _sha256_stream(source, should_cancel=None) -> str:
    digest = hashlib.sha256()
    while True:
        if should_cancel and should_cancel():
            raise _SendCancelled()
        chunk = source.read(_BUFFER)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _sha256_file(path: str, should_cancel=None) -> str:
    with open(path, "rb") as source:
        return _sha256_stream(source, should_cancel)


def _transfer_id(kind: str, name: str, plain_size: int, digest: str) -> str:
    identity = json.dumps(
        ["WHPP3", kind, name, plain_size, digest],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _load_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json_atomic(path: str, value: dict) -> None:
    temporary = path + f".{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _metadata_matches(value: dict, expected: dict) -> bool:
    return all(value.get(key) == item for key, item in expected.items())


def _validated_commit_destination(commit: dict, metadata: dict,
                                  target_root: str, part_path: str,
                                  is_folder: bool) -> str | None:
    """Return a safely committed destination after a crash, if it still matches."""
    if not _metadata_matches(commit, metadata):
        return None
    destination = commit.get("path")
    if not isinstance(destination, str) or not destination:
        return None
    destination = os.path.abspath(destination)
    try:
        if (os.path.realpath(os.path.dirname(destination))
                != os.path.realpath(target_root)):
            return None
        destination_mode = os.stat(destination, follow_symlinks=False).st_mode
        if is_folder:
            if not stat.S_ISDIR(destination_mode):
                return None
            part_stat = os.stat(part_path, follow_symlinks=False)
            if (not stat.S_ISREG(part_stat.st_mode)
                    or part_stat.st_size != metadata["plain_size"]
                    or not hmac.compare_digest(
                        _sha256_file(part_path), metadata["sha256"])):
                return None
        else:
            destination_stat = os.stat(destination, follow_symlinks=False)
            if (not stat.S_ISREG(destination_stat.st_mode)
                    or destination_stat.st_size != metadata["plain_size"]
                    or not hmac.compare_digest(
                        _sha256_file(destination), metadata["sha256"])):
                return None
    except (OSError, KeyError, TypeError):
        return None
    return destination


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


def _receive_folder_stream(reader, staging: str) -> None:
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
                 on_transfer_end: Callable[[str, str, bool], None] | None = None,
                 on_manual_peer_verified: Callable[[], None] | None = None,
                 on_trust_changed: Callable[[], None] | None = None):
        self.cfg = cfg
        self.on_sent = on_sent
        self.on_received = on_received
        self.on_status = on_status
        self.on_peers_changed = on_peers_changed
        self.on_progress = on_progress   # (kind:"send"/"recv", 文件名, 已传字节, 总字节)
        self.on_transfer_end = on_transfer_end  # (kind, 文件名, 是否完整完成)
        self.on_manual_peer_verified = on_manual_peer_verified
        self.on_trust_changed = on_trust_changed

        # 本节点唯一实例 ID：进服务名保证唯一(两台设备同名不再冲突)，
        # 进 TXT 属性用于"不发现自己"(比按显示名过滤可靠)。
        # 从 cfg 取(桌宠会持久化到 config.json)——同一设备重启用同一 ID，
        # 服务名不变，避免旧记录变成永不消失的"幽灵设备"。
        self._instance_id = cfg.instance_id
        try:
            self._identity = DeviceIdentity.from_private_key(
                cfg.identity_private_key)
        except (ValueError, TypeError):
            self._identity = DeviceIdentity.generate()
            cfg.identity_private_key = self._identity.export_private_key()

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
        self._checkpoint_lock = threading.Lock()
        self._checkpoint_ready = threading.Condition(self._checkpoint_lock)
        self._active_checkpoints: set[str] = set()
        self._outgoing_state_lock = threading.Lock()
        self._outgoing_state_path = os.path.join(
            self.cfg.inbox, ".inkhole-outgoing.json")

        # 对端存活探测(幽灵设备兜底)；参数做成实例属性主要为了测试提速
        self._probe_interval = _PROBE_INTERVAL
        self._probe_timeout = _PROBE_TIMEOUT
        self._probe_strikes = _PROBE_STRIKES
        self._probe_thread: threading.Thread | None = None
        self._probe_wake = threading.Event()
        self._pending_discovery_probes: dict[str, object] = {}
        self._identity_errors: set[str] = set()

        if cfg.active_secret:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            except ImportError:
                raise SystemExit("端到端加密(--secret)需要 cryptography 库：pip install cryptography")

        for root in inbox_roots(self.cfg):
            os.makedirs(root, exist_ok=True)
        self._cleanup_transfer_artifacts()

    def _cleanup_transfer_artifacts(self) -> None:
        """Remove abandoned WHPP checkpoints and receipts after seven days."""
        cutoff = time.time() - _CHECKPOINT_MAX_AGE
        groups: dict[str, list[str]] = {}
        for inbox_root in inbox_roots(self.cfg):
            try:
                names = os.listdir(inbox_root)
            except OSError:
                continue
            for name in names:
                if (not name.startswith(".inkhole-")
                        or name == ".inkhole-outgoing.json"):
                    continue
                path = os.path.join(inbox_root, name)
                suffix = name[len(".inkhole-"):]
                transfer_id = suffix.split(".", 1)[0]
                if _valid_sha256(transfer_id):
                    groups.setdefault(transfer_id, []).append(path)
                    continue
                try:
                    if os.path.getmtime(path) < cutoff:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                except OSError:
                    pass
        for transfer_id, paths in groups.items():
            try:
                newest = max(os.path.getmtime(path) for path in paths)
            except (OSError, ValueError):
                continue
            if newest >= cutoff:
                continue
            with self._checkpoint_lock:
                if transfer_id in self._active_checkpoints:
                    continue
            for path in paths:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.remove(path)
                except OSError:
                    pass

        with self._outgoing_state_lock:
            state = _load_json_file(self._outgoing_state_path)
            fresh = {}
            for key, value in state.items():
                try:
                    updated_at = int(value.get("updated_at", 0) or 0)
                except (AttributeError, TypeError, ValueError, OverflowError):
                    continue
                if updated_at >= cutoff:
                    fresh[key] = value
            if fresh != state:
                if fresh:
                    _write_json_atomic(self._outgoing_state_path, fresh)
                else:
                    try:
                        os.remove(self._outgoing_state_path)
                    except FileNotFoundError:
                        pass

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """启动：注册 mDNS + 启动 TCP 监听 + 开始发现其他节点。"""
        if self._running:
            return
        self._running = True

        # 1. 启动 TCP 监听
        try:
            self._start_tcp_server()
        except OSError as exc:
            self._running = False
            if self._server_sock:
                try:
                    self._server_sock.close()
                except OSError:
                    pass
                self._server_sock = None
            self._actual_port = 0
            if self.cfg.listen_port:
                self._status(
                    f"墨洞未开启：固定监听端口 {self.cfg.listen_port} 不可用",
                    str(exc))
            else:
                self._status("墨洞未开启：监听端口启动失败", str(exc))
            return

        # 2. 首轮立即验证手动设备；只有有效 WHPC v3 响应才会进入列表。
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
                b"whpc": str(_CAP_VERSION).encode("ascii"),
                b"caps": _FOLDER_KIND.encode("ascii"),
                b"identity": self._identity.fingerprint.encode("ascii"),
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
        self._probe_wake.set()
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
        # 固定端口是跨网配置的一部分。被占用时必须失败，不能静默换随机端口。
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
        """Receive one resumable WHPP v3 plaintext transaction."""
        part_path = ""
        meta_path = ""
        folder_staging = ""
        checkpoint_id = ""
        checkpoint_claimed = False
        ack_sent = False
        ok = False
        transfer_name = ""
        transfer_started = False
        core_authenticated = False
        try:
            conn.settimeout(_RECV_IDLE_TIMEOUT)
            # Probe connections that close before sending four bytes remain silent.
            magic = _recv_exact(conn, 4)
            if magic == _CORE_MAGIC:
                expected = self.cfg.core_ingress_token.encode("ascii")
                supplied = _recv_exact(conn, len(expected)) if expected else None
                if not supplied or not hmac.compare_digest(supplied, expected):
                    return
                core_authenticated = True
                magic = _recv_exact(conn, 4)
            if magic == _CAP_MAGIC:
                nonce = _recv_exact(conn, 32)
                if nonce is None:
                    return
                signature = self._identity.sign(capability_message(
                    nonce, self._instance_id, self.cfg.peer_name,
                    _CAP_VERSION, _CAPABILITIES))
                body = json.dumps(
                    {"version": _CAP_VERSION,
                     "caps": list(_CAPABILITIES),
                     "instance_id": self._instance_id,
                     "peer_name": self.cfg.peer_name,
                     "public_key": self._identity.public_key,
                     "signature": signature},
                    separators=(",", ":"),
                ).encode("utf-8")
                conn.sendall(_CAP_MAGIC + struct.pack("!I", len(body)) + body)
                return
            if magic != _MAGIC:
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
            version = header.get("version")
            plain_size = header.get("plain_size")
            transfer_id = str(header.get("transfer_id") or "").lower()
            expected_digest = str(header.get("sha256") or "").lower()
            sender_instance_id = str(
                header.get("sender_instance_id") or "").lower()
            sender_public_key = str(header.get("sender_public_key") or "")
            encrypted = bool(header.get("encrypted", False))
            enc_mode = str(header.get("enc_mode", ""))
            kind = str(header.get("kind", "file"))
            is_folder = kind == _FOLDER_KIND
            target_root = inbox_root_for(self.cfg, filename, kind)
            modified_ms = header.get("mtime_ms", 0)
            if version != _PROTOCOL_VERSION or not bool(header.get("want_ack", False)):
                self._status(f"拒收 {filename}：需要 WHPP v3")
                return
            if kind not in ("file", _FOLDER_KIND):
                self._status(f"拒收 {filename}：不支持的传输类型")
                return
            if (isinstance(plain_size, bool) or not isinstance(plain_size, int)
                    or not 0 <= plain_size <= _MAX_FILE_SIZE
                    or (is_folder and plain_size < 8)):
                self._status(f"拒收 {filename}：文件大小非法")
                return
            if (not _valid_sha256(transfer_id)
                    or not _valid_sha256(expected_digest)):
                self._status(f"拒收 {filename}：传输标识或摘要非法")
                return
            if not _valid_instance_id(sender_instance_id):
                self._status(f"拒收 {filename}：发送设备身份非法")
                return
            try:
                sender_fingerprint = public_fingerprint(sender_public_key)
            except ValueError:
                self._status(f"拒收 {filename}：发送设备公钥非法")
                return
            if self.cfg.trusted_only and not core_authenticated:
                with self._lock:
                    selected = (self._peers.get(self._selected_peer)
                                if self._selected_peer else None)
                pinned = self.cfg.trusted_peers.get(sender_instance_id, "")
                if (selected is None or selected.instance_id != sender_instance_id
                        or not pinned
                        or not hmac.compare_digest(pinned, sender_fingerprint)):
                    self._status(
                        f"已拒收 {addr[0]} 的传输（发送设备未配对或不是当前目标）")
                    try:
                        conn.sendall(_ACK_FAIL)
                    except OSError:
                        pass
                    return
            if (isinstance(modified_ms, bool) or not isinstance(modified_ms, int)
                    or not 0 <= modified_ms <= 0xFFFFFFFFFFFFFFFF):
                self._status(f"拒收 {filename}：修改时间非法")
                return
            if encrypted and enc_mode != "chunked":
                self._status(f"拒收 {filename}：加密格式不支持续传")
                return
            try:
                os.makedirs(target_root, exist_ok=True)
            except OSError as exc:
                self._status(f"拒收 {filename}：无法使用收件箱目录 ({exc})")
                return
            if encrypted and not self.cfg.active_secret:
                self._status(f"拒收 {filename}：对方启用了加密，本机未设口令")
                return

            checkpoint_id = transfer_id
            checkpoint_deadline = time.monotonic() + _RECV_IDLE_TIMEOUT
            with self._checkpoint_ready:
                while checkpoint_id in self._active_checkpoints:
                    remaining = checkpoint_deadline - time.monotonic()
                    if remaining <= 0:
                        self._status(f"拒收 {filename}：等待前一次传输结束超时")
                        return
                    self._checkpoint_ready.wait(remaining)
                self._active_checkpoints.add(checkpoint_id)
                checkpoint_claimed = True

            checkpoint_base = os.path.join(target_root, f".inkhole-{transfer_id}")
            part_path = checkpoint_base + ".part"
            meta_path = checkpoint_base + ".json"
            done_path = checkpoint_base + ".done.json"
            commit_path = checkpoint_base + ".commit.json"
            metadata = {
                "version": _PROTOCOL_VERSION,
                "filename": filename,
                "plain_size": plain_size,
                "sha256": expected_digest,
                "kind": kind,
                "mtime_ms": modified_ms,
                "sender_instance_id": sender_instance_id,
                "sender_fingerprint": sender_fingerprint,
            }

            def authenticate_sender(offset: int, nonce: bytes) -> None:
                length_bytes = _recv_exact(conn, 2)
                if length_bytes is None:
                    raise EOFError("发送设备签名缺失")
                signature_size = struct.unpack("!H", length_bytes)[0]
                if not 0 < signature_size <= 256:
                    raise ValueError("发送设备签名大小非法")
                signature = _recv_exact(conn, signature_size)
                if signature is None or not verify(
                        sender_public_key, transfer_message(nonce, header, offset),
                        signature.decode("ascii", errors="ignore")):
                    raise ValueError("发送设备身份签名无效")

            def send_receiver_challenge(offset: int) -> bytes:
                nonce = os.urandom(32)
                public_key = self._identity.public_key.encode("ascii")
                signature = self._identity.sign(receiver_message(
                    nonce, header, offset, self._instance_id)).encode("ascii")
                if (len(public_key) > _MAX_IDENTITY_FIELD
                        or len(signature) > _MAX_IDENTITY_FIELD):
                    raise ValueError("接收设备身份字段过大")
                conn.sendall(b"".join((
                    _RESUME, struct.pack("!Q", offset), nonce,
                    self._instance_id.encode("ascii"),
                    struct.pack("!H", len(public_key)), public_key,
                    struct.pack("!H", len(signature)), signature,
                )))
                return nonce

            recovered_destination = None
            commit = _load_json_file(commit_path)
            if commit:
                recovered_destination = _validated_commit_destination(
                    commit, metadata, target_root, part_path, is_folder)
                if not recovered_destination:
                    try:
                        os.remove(commit_path)
                    except OSError:
                        pass

            completed = _load_json_file(done_path)
            # The receipt proves that this transfer was atomically committed. The
            # delivered item may since have been moved, exported or deleted; tying
            # idempotency to its current path would duplicate a lost-ACK transfer.
            if (_metadata_matches(completed, metadata)
                    or recovered_destination is not None):
                nonce = send_receiver_challenge(plain_size)
                authenticate_sender(plain_size, nonce)
                body_size = _recv_exact(conn, 8)
                if body_size is None or struct.unpack("!Q", body_size)[0] != 0:
                    raise ValueError("已完成传输仍收到数据")
                if recovered_destination:
                    _apply_mtime(recovered_destination, modified_ms)
                    _write_json_atomic(done_path, commit)
                for obsolete in ((part_path,) if is_folder else ()) + (
                        meta_path, commit_path):
                    try:
                        os.remove(obsolete)
                    except OSError:
                        pass
                conn.sendall(_ACK_OK + bytes.fromhex(expected_digest))
                ack_sent = True
                ok = True
                if recovered_destination:
                    if self.on_received:
                        self.on_received(recovered_destination)
                    self._status(
                        f"已恢复并校验：{os.path.basename(recovered_destination)}")
                return

            existing = _load_json_file(meta_path)
            if existing != metadata:
                for stale in (part_path, meta_path):
                    try:
                        os.remove(stale)
                    except FileNotFoundError:
                        pass
                _write_json_atomic(meta_path, metadata)
            offset = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
            if offset > plain_size:
                os.remove(part_path)
                offset = 0
            remaining_disk = plain_size - offset
            required_disk = remaining_disk + _DISK_MARGIN
            if is_folder:
                # The completed WHF1 checkpoint and extracted tree coexist until
                # the directory has been validated and atomically committed.
                required_disk += plain_size
            if required_disk > shutil.disk_usage(target_root).free:
                self._status(f"拒收 {filename}：磁盘空间不足")
                return

            progress = _Progress(self.on_progress, "recv", filename, plain_size)
            transfer_name = filename
            transfer_started = True
            progress.update(offset)
            nonce = send_receiver_challenge(offset)
            authenticate_sender(offset, nonce)
            body_size_bytes = _recv_exact(conn, 8)
            if body_size_bytes is None:
                raise EOFError("续传数据长度缺失")
            body_size = struct.unpack("!Q", body_size_bytes)[0]
            remaining_plain = plain_size - offset
            expected_wire = (chunked_wire_size(remaining_plain)
                             if encrypted and remaining_plain else remaining_plain)
            if body_size != expected_wire:
                raise ValueError("续传数据长度不一致")

            if remaining_plain:
                with open(part_path, "ab") as output:
                    appended = 0
                    if encrypted:
                        hdr32 = _recv_exact(conn, 32)
                        if hdr32 is None:
                            raise EOFError("加密流头不完整")
                        decryptor = ChunkedDecryptor(self.cfg.active_secret, hdr32)
                        consumed = 32
                        while consumed < body_size:
                            length_bytes = _recv_exact(conn, 4)
                            if length_bytes is None:
                                raise EOFError("加密数据不完整")
                            ciphertext_size = struct.unpack("!I", length_bytes)[0]
                            if (not 16 <= ciphertext_size <= CHUNK_SIZE + 16
                                    or consumed + 4 + ciphertext_size > body_size):
                                raise ValueError("加密分块非法")
                            ciphertext = _recv_exact(conn, ciphertext_size)
                            if ciphertext is None:
                                raise EOFError("加密数据不完整")
                            plain = decryptor.decrypt_chunk(ciphertext)
                            if plain is None:
                                raise ValueError("解密失败（两端口令不一致？）")
                            if appended + len(plain) > remaining_plain:
                                raise ValueError("解密数据超过声明大小")
                            output.write(plain)
                            appended += len(plain)
                            consumed += 4 + ciphertext_size
                            progress.update(offset + appended)
                        if consumed != body_size or appended != remaining_plain:
                            raise EOFError("加密数据不完整")
                    else:
                        while appended < remaining_plain:
                            chunk = conn.recv(min(_BUFFER, remaining_plain - appended))
                            if not chunk:
                                raise EOFError("文件数据不完整")
                            output.write(chunk)
                            appended += len(chunk)
                            progress.update(offset + appended)
                    output.flush()
                    os.fsync(output.fileno())
            elif not os.path.exists(part_path):
                with open(part_path, "xb") as output:
                    output.flush()
                    os.fsync(output.fileno())

            if not os.path.isfile(part_path) or os.path.getsize(part_path) != plain_size:
                raise EOFError("文件数据不完整")
            actual_digest = _sha256_file(part_path)
            if not hmac.compare_digest(actual_digest, expected_digest):
                try:
                    os.remove(part_path)
                finally:
                    try:
                        os.remove(meta_path)
                    except FileNotFoundError:
                        pass
                raise ValueError("文件 SHA-256 校验失败，已丢弃检查点")

            if is_folder:
                folder_staging = os.path.join(
                    target_root, f".inkhole-{uuid.uuid4().hex}.folder.part")
                os.mkdir(folder_staging)
                reader = _ExactFileReader(part_path, plain_size)
                try:
                    _receive_folder_stream(reader, folder_staging)
                except Exception:
                    for invalid in (part_path, meta_path):
                        try:
                            os.remove(invalid)
                        except FileNotFoundError:
                            pass
                    raise
                finally:
                    reader.close()
                _apply_mtime(folder_staging, modified_ms)
                with self._lock:
                    dst = _unique_directory_path(target_root, filename)
                    receipt = dict(metadata)
                    receipt["path"] = dst
                    receipt["completed_at"] = int(time.time())
                    _write_json_atomic(commit_path, receipt)
                    os.replace(folder_staging, dst)
                folder_staging = ""
            else:
                with self._lock:
                    dst = _unique_path(target_root, filename)
                    receipt = dict(metadata)
                    receipt["path"] = dst
                    receipt["completed_at"] = int(time.time())
                    _write_json_atomic(commit_path, receipt)
                    os.replace(part_path, dst)
                _apply_mtime(dst, modified_ms)

            _write_json_atomic(done_path, receipt)
            for obsolete in ((part_path,) if is_folder else ()) + (
                    meta_path, commit_path):
                try:
                    os.remove(obsolete)
                except OSError:
                    pass
            conn.sendall(_ACK_OK + bytes.fromhex(expected_digest))
            ack_sent = True
            ok = True
            if self.on_received:
                self.on_received(dst)
            self._status(f"已接收并校验：{os.path.basename(dst)}")
        except (EOFError, ConnectionResetError, ConnectionAbortedError):
            self._status(f"接收中断，已保留续传进度：{transfer_name or '未知文件'}")
        except Exception as e:
            self._status("接收失败", str(e))
        finally:
            if folder_staging:
                shutil.rmtree(folder_staging, ignore_errors=True)
            if transfer_started and not ack_sent:
                try:
                    conn.sendall(_ACK_FAIL)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
            if checkpoint_claimed:
                with self._checkpoint_ready:
                    self._active_checkpoints.discard(checkpoint_id)
                    self._checkpoint_ready.notify_all()
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
        """Return cached verified capabilities, probing only injected/test peers."""
        if peer.capabilities and peer.identity_fingerprint:
            return set(peer.capabilities)
        hosts = sorted(peer.hosts or [peer.host],
                       key=lambda host: 1 if _is_tailnet_ip(host) else 0)
        for host in hosts:
            try:
                result = _probe_peer(host, peer.port, _CAP_TIMEOUT,
                                     peer.instance_id,
                                     self.cfg.trusted_peers.get(peer.instance_id, ""))
                peer.instance_id = result.instance_id
                peer.capabilities = result.capabilities
                peer.public_key = result.public_key
                peer.identity_fingerprint = result.fingerprint
                return set(result.capabilities)
            except OSError:
                continue
        return set()

    def _ensure_receiver_identity(self, peer: PeerInfo) -> None:
        """Direct transports require a verified receiver pin before data is sent."""
        if peer.transport not in {"lan", "tailscale"}:
            return
        expected = (peer.identity_fingerprint
                    or self.cfg.trusted_peers.get(peer.instance_id, ""))
        if not peer.instance_id or not expected:
            self._probe_peer_capabilities(peer)
            expected = (peer.identity_fingerprint
                        or self.cfg.trusted_peers.get(peer.instance_id, ""))
        if not peer.instance_id or not expected:
            raise OSError("接收设备身份尚未验证")

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

        # A peer without folder-v1 can still receive a folder as one ZIP file.
        # Build it in the queue worker (never the GUI thread), then always clean up.
        self._status("对端不支持文件夹流，正在打包文件夹…")
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

    def _connect_for_transfer(self, peer: PeerInfo,
                              route_offset: int = 0) -> socket.socket:
        routes = self._transfer_route_candidates(peer)
        if len(routes) > 1:
            offset = route_offset % len(routes)
            routes = routes[offset:] + routes[:offset]
        last_error: OSError | None = None
        for route in routes:
            hosts = sorted(route.hosts or [route.host],
                           key=lambda host: 1 if _is_tailnet_ip(host) else 0)
            for host in hosts:
                try:
                    sock = _connect_peer_socket(route, host, _CONNECT_TIMEOUT)
                    if route is not peer:
                        self._status(
                            f"当前通道不可用，已切换至{self._route_label(route)}")
                    return sock
                except OSError as exc:
                    last_error = exc
        raise last_error if last_error else OSError("无可用地址")

    @staticmethod
    def _route_label(peer: PeerInfo) -> str:
        return {
            "ssh": " SSH 中继",
            "lan": "局域网",
            "tailscale": " Tailscale",
        }.get(peer.transport, "备用通道")

    def _transfer_route_candidates(self, selected: PeerInfo) -> list[PeerInfo]:
        """Return authenticated routes for the same physical device."""
        if (not _valid_instance_id(selected.instance_id)
                or selected.transport == "wormhole"):
            return [selected]
        with self._lock:
            peers = list(self._peers.values())
        pinned = self.cfg.trusted_peers.get(selected.instance_id, "")

        def usable(candidate: PeerInfo) -> bool:
            if candidate is selected:
                return True
            if candidate.instance_id != selected.instance_id:
                return False
            if candidate.transport == "ssh":
                return bool(candidate.endpoint_token)
            return (candidate.transport in {"lan", "tailscale"}
                    and bool(pinned)
                    and hmac.compare_digest(
                        candidate.identity_fingerprint, pinned))

        priority = {"ssh": 0, "lan": 1, "tailscale": 2}
        alternatives = [candidate for candidate in peers if usable(candidate)]
        alternatives.sort(key=lambda candidate: (
            0 if candidate is selected else 1,
            priority.get(candidate.transport, 9),
            candidate.service_name,
        ))
        return alternatives or [selected]

    def _outgoing_transfer(self, peer: PeerInfo, local_path: str, kind: str,
                           name: str, plain_size: int, digest: str) -> tuple[str, str]:
        key_data = json.dumps([
            os.path.abspath(local_path), kind, name, plain_size, digest,
            peer.instance_id or f"{peer.host}:{peer.port}",
        ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(key_data).hexdigest()
        with self._outgoing_state_lock:
            state = _load_json_file(self._outgoing_state_path)
            record = state.get(key) if isinstance(state.get(key), dict) else {}
            transfer_id = str(record.get("transfer_id") or "").lower()
            if not _valid_sha256(transfer_id):
                transfer_id = hashlib.sha256(os.urandom(32)).hexdigest()
            state[key] = {"transfer_id": transfer_id, "updated_at": int(time.time())}
            _write_json_atomic(self._outgoing_state_path, state)
        return key, transfer_id

    def _complete_outgoing_transfer(self, key: str) -> None:
        with self._outgoing_state_lock:
            state = _load_json_file(self._outgoing_state_path)
            if key not in state:
                return
            state.pop(key, None)
            if state:
                _write_json_atomic(self._outgoing_state_path, state)
            else:
                try:
                    os.remove(self._outgoing_state_path)
                except FileNotFoundError:
                    pass

    def _send_resumable_payload(self, peer: PeerInfo, header_dict: dict,
                                source_factory, cancellation_requested,
                                progress) -> bool:
        header_dict = dict(header_dict)
        header_dict["sender_instance_id"] = self._instance_id
        header_dict["sender_public_key"] = self._identity.public_key
        header = json.dumps(
            header_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        plain_size = int(header_dict["plain_size"])
        encrypted = bool(header_dict["encrypted"])
        expected_digest = str(header_dict["sha256"])
        last_error: Exception | None = None

        self._ensure_receiver_identity(peer)

        for attempt in range(_TRANSFER_RETRIES):
            if cancellation_requested():
                raise _SendCancelled()
            sock = None
            try:
                sock = self._connect_for_transfer(peer, attempt)
                with self._send_state_lock:
                    self._active_send_sock = sock
                sock.settimeout(_SEND_IO_TIMEOUT)
                sock.sendall(_MAGIC + struct.pack("!I", len(header)) + header)

                marker = _recv_exact_cancellable(
                    sock, 1, cancellation_requested, _SEND_IO_TIMEOUT)
                if marker == _ACK_FAIL:
                    raise _ReceiverRejected("接收方拒绝了传输")
                if marker != _RESUME:
                    raise OSError("接收方未返回 WHPP v3 续传状态")
                offset_bytes = _recv_exact_cancellable(
                    sock, 8, cancellation_requested, _SEND_IO_TIMEOUT)
                if offset_bytes is None:
                    raise OSError("接收方续传状态不完整")
                offset = struct.unpack("!Q", offset_bytes)[0]
                if offset > plain_size:
                    raise OSError("接收方续传偏移非法")
                nonce = _recv_exact_cancellable(
                    sock, 32, cancellation_requested, _SEND_IO_TIMEOUT)
                if nonce is None:
                    raise OSError("接收方身份挑战不完整")
                receiver_instance_raw = _recv_exact_cancellable(
                    sock, 32, cancellation_requested, _SEND_IO_TIMEOUT)
                if receiver_instance_raw is None:
                    raise OSError("接收设备实例标识不完整")
                try:
                    receiver_instance_id = receiver_instance_raw.decode("ascii").lower()
                except UnicodeDecodeError as exc:
                    raise OSError("接收设备实例标识无效") from exc
                public_size_raw = _recv_exact_cancellable(
                    sock, 2, cancellation_requested, _SEND_IO_TIMEOUT)
                if public_size_raw is None:
                    raise OSError("接收设备公钥不完整")
                public_size = struct.unpack("!H", public_size_raw)[0]
                if not 0 < public_size <= _MAX_IDENTITY_FIELD:
                    raise OSError("接收设备公钥大小非法")
                receiver_public_raw = _recv_exact_cancellable(
                    sock, public_size, cancellation_requested, _SEND_IO_TIMEOUT)
                signature_size_raw = _recv_exact_cancellable(
                    sock, 2, cancellation_requested, _SEND_IO_TIMEOUT)
                if receiver_public_raw is None or signature_size_raw is None:
                    raise OSError("接收设备身份响应不完整")
                signature_size = struct.unpack("!H", signature_size_raw)[0]
                if not 0 < signature_size <= _MAX_IDENTITY_FIELD:
                    raise OSError("接收设备签名大小非法")
                receiver_signature_raw = _recv_exact_cancellable(
                    sock, signature_size, cancellation_requested, _SEND_IO_TIMEOUT)
                if receiver_signature_raw is None:
                    raise OSError("接收设备签名不完整")
                try:
                    receiver_public = receiver_public_raw.decode("ascii")
                    receiver_signature = receiver_signature_raw.decode("ascii")
                    receiver_fingerprint = public_fingerprint(receiver_public)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise OSError("接收设备身份响应无效") from exc
                expected_fingerprint = (peer.identity_fingerprint
                                        or self.cfg.trusted_peers.get(
                                            peer.instance_id, ""))
                if (not _valid_instance_id(receiver_instance_id)
                        or (peer.instance_id
                            and receiver_instance_id != peer.instance_id)
                        or (expected_fingerprint and not hmac.compare_digest(
                            receiver_fingerprint, expected_fingerprint))
                        or not verify(receiver_public, receiver_message(
                            nonce, header_dict, offset, receiver_instance_id),
                            receiver_signature)):
                    raise OSError("接收设备身份验证失败")
                signature = self._identity.sign(
                    transfer_message(nonce, header_dict, offset)).encode("ascii")
                sock.sendall(struct.pack("!H", len(signature)) + signature)
                if offset:
                    percent = offset * 100 // max(1, plain_size)
                    self._status(f"正在续传 {header_dict['filename']} · {percent}%")
                progress.update(offset)

                remaining = plain_size - offset
                wire_size = (chunked_wire_size(remaining)
                             if encrypted and remaining else remaining)
                sock.sendall(struct.pack("!Q", wire_size))
                if remaining:
                    with source_factory(offset) as raw_source:
                        source = _ProgressReader(raw_source, progress, offset)
                        if encrypted:
                            sent = 0
                            for blob in encrypt_chunks(self.cfg.active_secret, source):
                                if cancellation_requested():
                                    raise _SendCancelled()
                                sock.sendall(blob)
                                sent += len(blob)
                            if sent != wire_size:
                                raise OSError("加密发送大小不一致")
                        else:
                            sent = 0
                            while sent < remaining:
                                if cancellation_requested():
                                    raise _SendCancelled()
                                chunk = source.read(min(_BUFFER, remaining - sent))
                                if not chunk:
                                    raise OSError("发送源数据不完整")
                                sock.sendall(chunk)
                                sent += len(chunk)

                sock.settimeout(_RECV_IDLE_TIMEOUT)
                ack = _recv_exact_cancellable(
                    sock, 1, cancellation_requested, _RECV_IDLE_TIMEOUT)
                if ack == _ACK_FAIL:
                    raise _ReceiverRejected("接收方校验或落盘失败")
                if ack != _ACK_OK:
                    raise OSError("未收到接收成功回执")
                received_digest = _recv_exact_cancellable(
                    sock, _DIGEST_SIZE, cancellation_requested, _SEND_IO_TIMEOUT)
                if (received_digest is None
                        or not hmac.compare_digest(received_digest.hex(), expected_digest)):
                    raise OSError("接收方 SHA-256 回执不一致")
                return True
            except (_ReceiverRejected, _SendCancelled):
                raise
            except (ConnectionError, EOFError, socket.timeout, OSError) as exc:
                last_error = exc
                if cancellation_requested():
                    raise _SendCancelled() from exc
                if attempt + 1 < _TRANSFER_RETRIES:
                    self._status(f"连接中断，正在恢复传输（{attempt + 2}/{_TRANSFER_RETRIES}）")
                    time.sleep(0.5 * (attempt + 1))
            finally:
                with self._send_state_lock:
                    if self._active_send_sock is sock:
                        self._active_send_sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        raise last_error if last_error else OSError("传输连接失败")

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
            self._status(f"正在扫描并校验文件夹：{name}")
            manifest = _scan_folder(local_path, cancellation_requested)
            with _FolderPayloadReader(manifest) as source:
                digest = _sha256_stream(source, cancellation_requested)
            outgoing_key, transfer_id = self._outgoing_transfer(
                peer, local_path, _FOLDER_KIND, name, manifest.plain_size, digest)
            encrypted = bool(self.cfg.active_secret)
            header = {
                "version": _PROTOCOL_VERSION,
                "filename": name,
                "plain_size": manifest.plain_size,
                "transfer_id": transfer_id,
                "sha256": digest,
                "kind": _FOLDER_KIND,
                "mtime_ms": manifest.root_mtime_ms,
                "encrypted": encrypted,
                "want_ack": True,
            }
            if encrypted:
                header["enc_mode"] = "chunked"

            def source_factory(offset: int):
                source = _FolderPayloadReader(manifest)
                try:
                    _discard_exact(source, offset)
                    return source
                except Exception:
                    source.close()
                    raise

            progress = _Progress(self.on_progress, "send", name, manifest.plain_size)
            completed = self._send_resumable_payload(
                peer, header, source_factory, cancellation_requested, progress)
            self._complete_outgoing_transfer(outgoing_key)
            if self.on_sent:
                self.on_sent(name)
            self._status(f"已发送并校验：{name}")
            return True
        except _SendCancelled:
            self._status(f"已取消发送：{name}")
            return False
        except _ReceiverRejected as exc:
            self._status(f"{peer.name} 接收失败", str(exc))
            return False
        except Exception as exc:
            if cancellation_requested():
                self._status(f"已取消发送：{name}")
            else:
                self._status("文件夹发送失败", str(exc))
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
        """Send one file with resumable checkpoints and verified completion."""
        if not os.path.isfile(local_path):
            self._status("文件不存在")
            return False

        selected, peer = self._selected_send_peer()
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
            plain_size = os.path.getsize(local_path)
            self._status(f"正在校验：{name}")
            digest = _sha256_file(local_path, cancellation_requested)
            outgoing_key, transfer_id = self._outgoing_transfer(
                peer, local_path, "file", name, plain_size, digest)
            encrypted = bool(self.cfg.active_secret)
            header = {
                "version": _PROTOCOL_VERSION,
                "filename": name,
                "plain_size": plain_size,
                "transfer_id": transfer_id,
                "sha256": digest,
                "kind": "file",
                "mtime_ms": max(0, int(os.path.getmtime(local_path) * 1000)),
                "encrypted": encrypted,
                "want_ack": True,
            }
            if encrypted:
                header["enc_mode"] = "chunked"

            def source_factory(offset: int):
                source = open(local_path, "rb")
                source.seek(offset)
                return source

            progress = _Progress(self.on_progress, "send", name, plain_size)
            completed = self._send_resumable_payload(
                peer, header, source_factory, cancellation_requested, progress)
            self._complete_outgoing_transfer(outgoing_key)
            if self.on_sent:
                self.on_sent(name)
            self._status(f"已发送并校验：{name}")
            return True
        except _SendCancelled:
            self._status(f"已取消发送：{name}")
            return False
        except _ReceiverRejected as exc:
            self._status(f"{peer.name} 接收失败", str(exc))
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
        """选择发送目标，并固定已验证的设备公钥指纹。"""
        trust_changed = False
        with self._lock:
            if name is None:
                self._selected_peer = None
                self._last_selected_service = None
            elif name in self._peers:
                self._selected_peer = name
                peer = self._peers[name]
                # 智能保留：记住 service_name，离线后重新上线能自动恢复选中
                self._last_selected_service = peer.service_name
                if (peer.instance_id and peer.identity_fingerprint
                        and self.cfg.trusted_peers.get(peer.instance_id)
                        != peer.identity_fingerprint):
                    self.cfg.trusted_peers[peer.instance_id] = peer.identity_fingerprint
                    trust_changed = True
        if trust_changed and self.on_trust_changed:
            try:
                self.on_trust_changed()
            except Exception:
                pass
        self._status(f"目标: {name}" if name else "未选择目标")

    def trusted_devices(self) -> dict[str, str]:
        """Return a copy of the persistent instance-id to fingerprint pins."""
        return dict(self.cfg.trusted_peers)

    def revoke_trust(self, instance_id: str) -> bool:
        """Revoke one device pin; the device must be selected again to pair."""
        removed = self.cfg.trusted_peers.pop(str(instance_id).lower(), None) is not None
        if removed and self.on_trust_changed:
            try:
                self.on_trust_changed()
            except Exception:
                pass
        return removed

    def _on_peer_added(self, name: str, host: str, port: int, service_name: str = "",
                       hosts: list[str] | None = None, instance_id: str = "",
                       capabilities: set[str] | frozenset[str] | None = None,
                       manual: bool = False, transport: str = "",
                       endpoint_token: str = "", public_key: str = "",
                       identity_fingerprint: str = "") -> None:
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
                        fresh = PeerInfo(p.name, host, port, service_name, hosts,
                                         instance_id, capabilities, manual,
                                         transport, endpoint_token, public_key,
                                         identity_fingerprint)
                        p.host, p.port, p.hosts = fresh.host, fresh.port, fresh.hosts
                        p.instance_id = fresh.instance_id
                        p.capabilities = fresh.capabilities
                        p.manual = fresh.manual
                        p.transport = fresh.transport
                        p.endpoint_token = fresh.endpoint_token
                        p.public_key = fresh.public_key
                        p.identity_fingerprint = fresh.identity_fingerprint
                        display_name = p.name
                        updated = True
                        break
            if not updated:
                display = name
                n = 2
                while display in self._peers and self._peers[display].service_name != service_name:
                    display = f"{name} ({n})"
                    n += 1
                self._peers[display] = PeerInfo(
                    display, host, port, service_name, hosts,
                    instance_id, capabilities, manual, transport,
                    endpoint_token, public_key, identity_fingerprint)
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

    # ---------- 外部传输核心端点（短码 / SSH） ----------
    def upsert_external_peer(self, peer_id: str, name: str, host: str, port: int,
                             transport: str, endpoint_token: str,
                             instance_id: str = "") -> str:
        """Expose one authenticated loopback endpoint as a normal WHPP peer."""
        peer_id = str(peer_id).strip()
        transport = str(transport).strip().lower()
        if not peer_id or transport not in {"wormhole", "ssh"}:
            raise ValueError("跨网设备标识或通道无效")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("跨网核心端点必须位于本机")
        port = int(port)
        if not 1 <= port <= 65535 or not endpoint_token:
            raise ValueError("跨网核心端点无效")
        service_name = f"external|{transport}|{peer_id}"
        self._on_peer_added(
            str(name).strip() or ("一次性接收端" if transport == "wormhole" else "SSH 设备"),
            host, port, service_name=service_name, hosts=[host],
            instance_id=str(instance_id).lower(), capabilities={_FOLDER_KIND},
            manual=True, transport=transport, endpoint_token=endpoint_token)
        with self._lock:
            return next((peer.name for peer in self._peers.values()
                         if peer.service_name == service_name), "")

    def remove_external_peer(self, peer_id: str, transport: str) -> None:
        service_name = f"external|{str(transport).strip().lower()}|{str(peer_id).strip()}"
        with self._lock:
            display = next((name for name, peer in self._peers.items()
                            if peer.service_name == service_name), None)
        if display:
            self._on_peer_removed(display)

    def _on_peer_removed_by_service(self, service_name: str) -> None:
        """mDNS 节点离线时调用：按唯一服务名精确匹配，找不到再回退按名字解析。

        显示名可能被去重加过后缀、或与服务名前缀不一致(TXT 里的 peer_name)，
        只有 service_name 能可靠对上离线事件，否则会留下永不消失的幽灵设备。
        """
        with self._lock:
            self._pending_discovery_probes.pop(service_name, None)
        display = None
        with self._lock:
            for n, p in self._peers.items():
                if p.service_name == service_name:
                    display = n
                    break
        if display is None:
            # Directly injected/test peers may not carry a service key.
            suffix = "." + _SERVICE_TYPE
            fallback = (service_name[:-len(suffix)]
                        if service_name.endswith(suffix)
                        else service_name.split(".")[0])
            with self._lock:
                if fallback in self._peers and not self._peers[fallback].service_name:
                    display = fallback
        if display is None:
            return
        self._on_peer_removed(display)

    # ---------- 手动设备(Tailscale/固定 IP 直连) ----------
    @staticmethod
    def _manual_key(entry: dict) -> str:
        """手动设备的稳定标识,顶替 mDNS service_name 参与增删/探测去重。"""
        return f"manual|{entry['host']}|{int(entry['port'])}"

    def _bind_manual_identity(self, entry: dict, result: _ProbeResult) -> None:
        pinned = str(entry.get("instance_id") or "").lower()
        if pinned and pinned != result.instance_id:
            raise _IdentityMismatch("设备身份已变化，请删除后重新添加")
        if pinned:
            trusted = self.cfg.trusted_peers.get(pinned, "")
            if trusted and trusted != result.fingerprint:
                raise _IdentityMismatch("设备密钥已变化，请撤销信任后重新配对")
            return
        entry["instance_id"] = result.instance_id
        if self.on_manual_peer_verified:
            try:
                self.on_manual_peer_verified()
            except Exception:
                pass

    def _register_manual(self, entry: dict, result: _ProbeResult) -> None:
        configured_host = str(entry["host"])
        addresses = _resolved_addresses([configured_host])
        if result.connected_address not in addresses:
            addresses.insert(0, result.connected_address)
        self._on_peer_added(
            str(entry.get("name") or result.peer_name or configured_host),
            result.connected_address, int(entry["port"]),
            service_name=self._manual_key(entry), hosts=addresses,
            instance_id=result.instance_id, capabilities=result.capabilities,
            manual=True, public_key=result.public_key,
            identity_fingerprint=result.fingerprint)

    def add_manual_peer(self, name: str, host: str, port: int) -> None:
        """新增或更新手动设备；有效 WHPC v3 响应后才进入在线列表。"""
        port = int(port)
        existing = next((m for m in (self.cfg.manual_peers or [])
                         if m["host"] == host and int(m["port"]) == port), None)
        entry = {"name": name, "host": host, "port": port}
        if existing and _valid_instance_id(existing.get("instance_id")):
            entry["instance_id"] = str(existing["instance_id"]).lower()
        self.cfg.manual_peers = [
            m for m in (self.cfg.manual_peers or [])
            if not (m["host"] == host and int(m["port"]) == port)]
        self.cfg.manual_peers.append(entry)
        if self._running:
            self._probe_wake.set()

    def remove_manual_peer(self, host: str, port: int) -> None:
        """删除手动设备:同时移出配置与在线列表。调用方负责持久化配置。"""
        port = int(port)
        self.cfg.manual_peers = [
            m for m in (self.cfg.manual_peers or [])
            if not (m["host"] == host and int(m["port"]) == port)]
        key = f"manual|{host}|{port}"
        self._identity_errors.discard(key)
        display = None
        with self._lock:
            for n, p in self._peers.items():
                if p.service_name == key:
                    display = n
                    break
        if display is not None:
            self._on_peer_removed(display)
        self._probe_wake.set()

    def _probe_hosts(self, hosts: list[str], port: int, timeout: float,
                     expected_instance_id: str = "",
                     expected_fingerprint: str = "") -> _ProbeResult:
        last_error: OSError | None = None
        tailnet_error: _TailnetUnavailable | None = None
        for host in hosts:
            try:
                return _probe_peer(host, port, timeout, expected_instance_id,
                                   expected_fingerprint)
            except _IdentityMismatch:
                raise
            except _TailnetUnavailable as exc:
                tailnet_error = exc
            except OSError as exc:
                last_error = exc
        if tailnet_error is not None:
            raise tailnet_error
        raise last_error if last_error else OSError("目标设备没有可用地址")

    def _verify_discovered_peer(self, name: str, hosts: list[str], port: int,
                                service_name: str, instance_id: str) -> None:
        """Verify mDNS metadata over WHPC before exposing a peer to the UI."""
        if not _valid_instance_id(instance_id):
            return
        candidates = self._hosts_on_current_lan(hosts)
        if not candidates:
            return
        token = object()
        with self._lock:
            if service_name in self._pending_discovery_probes:
                return
            self._pending_discovery_probes[service_name] = token

        def worker() -> None:
            try:
                result = self._probe_hosts(
                    candidates, port, self._probe_timeout, instance_id,
                    self.cfg.trusted_peers.get(instance_id, ""))
                with self._lock:
                    current = self._pending_discovery_probes.get(service_name)
                if self._running and current is token:
                    addresses = _resolved_addresses(candidates)
                    if result.connected_address not in addresses:
                        addresses.insert(0, result.connected_address)
                    self._on_peer_added(
                        name, result.connected_address, port, service_name,
                        addresses, result.instance_id, result.capabilities, False,
                        public_key=result.public_key,
                        identity_fingerprint=result.fingerprint)
            except OSError:
                pass
            finally:
                with self._lock:
                    if self._pending_discovery_probes.get(service_name) is token:
                        self._pending_discovery_probes.pop(service_name, None)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 对端存活探测 ----------
    def _probe_loop(self) -> None:
        """WHPC v3 liveness loop for verified discovered and manual peers."""
        strikes: dict[str, int] = {}   # service_name(或显示名) -> 连续失败轮数
        while self._running:
            with self._lock:
                # SSH/短码回环端点由跨网核心维护；LAN 子网过滤会固定排除
                # 127.0.0.1，若在这里探活会把正常的 SSH 配对设备误删。
                targets = [(p.service_name or n, p, n)
                           for n, p in self._peers.items()
                           if not (p.service_name or "").startswith("external|")]
            manual_by_key = {
                self._manual_key(entry): entry
                for entry in list(self.cfg.manual_peers or [])
            }
            seen = set()
            auto_removed = False
            for key, peer, display in targets:
                seen.add(key)
                entry = manual_by_key.get(key)
                is_manual = entry is not None
                hosts = (([str(entry["host"]), peer.host] + list(peer.hosts))
                         if entry is not None
                         else (self._hosts_on_current_lan(list(peer.hosts))
                               if peer.service_name else list(peer.hosts)))
                hosts = list(dict.fromkeys(hosts))
                probe_timeout = (self._probe_timeout * 2
                                 if is_manual else self._probe_timeout)
                expected = (str(entry.get("instance_id") or "")
                            if entry is not None else peer.instance_id)
                tailnet_unavailable = False
                try:
                    result = self._probe_hosts(
                        hosts, peer.port, probe_timeout, expected,
                        self.cfg.trusted_peers.get(expected, ""))
                    if entry is not None:
                        self._bind_manual_identity(entry, result)
                        addresses = _resolved_addresses([str(entry["host"])])
                        if result.connected_address not in addresses:
                            addresses.insert(0, result.connected_address)
                        peer.host = result.connected_address
                        peer.hosts = addresses
                    peer.instance_id = result.instance_id
                    peer.capabilities = result.capabilities
                    peer.public_key = result.public_key
                    peer.identity_fingerprint = result.fingerprint
                    strikes.pop(key, None)
                    self._identity_errors.discard(key)
                    continue
                except _IdentityMismatch as exc:
                    strikes.pop(key, None)
                    report_error = key not in self._identity_errors
                    if report_error:
                        self._identity_errors.add(key)
                    self._on_peer_removed(display)
                    if report_error:
                        self._status(f"{display} 身份验证失败", str(exc))
                    continue
                except OSError as exc:
                    tailnet_unavailable = isinstance(exc, _TailnetUnavailable)
                threshold = (1 if is_manual and tailnet_unavailable
                             else self._probe_strikes * 2
                             if is_manual else self._probe_strikes)
                if strikes.get(key, 0) + 1 >= threshold:
                    strikes.pop(key, None)
                    self._on_peer_removed(display)
                    if not is_manual:
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

            # Offline manual entries are verified before being exposed to the UI.
            with self._lock:
                known = {p.service_name for p in self._peers.values()}
            for entry in list(self.cfg.manual_peers or []):
                if not self._running:
                    break
                key = self._manual_key(entry)
                if key in known:
                    continue
                try:
                    expected = str(entry.get("instance_id") or "")
                    result = _probe_peer(str(entry["host"]), int(entry["port"]),
                                         self._probe_timeout * 2, expected,
                                         self.cfg.trusted_peers.get(expected, ""))
                    self._bind_manual_identity(entry, result)
                except _IdentityMismatch as exc:
                    if key not in self._identity_errors:
                        self._identity_errors.add(key)
                        label = str(entry.get("name") or entry["host"])
                        self._status(f"{label} 身份验证失败", str(exc))
                    continue
                except OSError:
                    continue
                self._identity_errors.discard(key)
                self._register_manual(entry, result)

            self._probe_wake.wait(self._probe_interval)
            self._probe_wake.clear()

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
    def _local_lan_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        try:
            import psutil
            for iface, addrs in psutil.net_if_addrs().items():
                if any(keyword in iface.lower()
                       for keyword in _VIRTUAL_INTERFACE_KEYWORDS):
                    continue
                for addr in addrs:
                    if addr.family not in (socket.AF_INET, socket.AF_INET6):
                        continue
                    raw = str(addr.address).split("%", 1)[0]
                    parsed = _plain_ip(raw)
                    if (parsed is None or parsed.is_loopback or parsed.is_link_local
                            or _is_tailnet_ip(raw) or not addr.netmask):
                        continue
                    try:
                        network = ipaddress.ip_network(
                            f"{raw}/{addr.netmask}", strict=False)
                    except ValueError:
                        continue
                    if network not in networks:
                        networks.append(network)
        except (ImportError, OSError):
            pass
        return networks

    def _hosts_on_current_lan(self, hosts: list[str]) -> list[str]:
        networks = self._local_lan_networks()
        candidates: list[str] = []
        for address in _resolved_addresses(hosts):
            parsed = _plain_ip(address)
            if parsed is None or parsed.is_loopback or _is_tailnet_ip(address):
                continue
            if not networks or any(parsed in network for network in networks):
                candidates.append(address)
        return candidates

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
            for iface, addrs in psutil.net_if_addrs().items():
                if any(kw in iface.lower() for kw in _VIRTUAL_INTERFACE_KEYWORDS):
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

        instance_id = str(props.get("instance_id", "")).lower()
        if (not _valid_instance_id(instance_id)
                or str(props.get("whpc", "")) != str(_CAP_VERSION)):
            return
        if instance_id == self._node._instance_id:
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

        self._node._verify_discovered_peer(
            peer_name, list(addresses), info.port, name, instance_id)

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


def _recv_exact_cancellable(sock: socket.socket, n: int, should_cancel,
                            timeout: float) -> bytes | None:
    """Read a control frame while guaranteeing prompt cross-thread cancel."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < n:
        if should_cancel():
            raise _SendCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        try:
            sock.settimeout(min(0.5, remaining))
            chunk = sock.recv(n - len(data))
        except socket.timeout:
            continue
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


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
