"""SSH-only remote transport using OpenSSH forwarding and short-lived metadata."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .p2p import _safe_filename, _unique_path, _Progress
from .relay_crypto import (DeviceIdentity, FRAME_PLAIN_LIMIT, RelayCipher,
                           derive_transfer_key)

_MAGIC = b"WHPP"
_HANDSHAKE_MAGIC = b"ISSH"
_PROTOCOL_VERSION = 1
_MAX_HEADER = 64 * 1024
_MAX_HANDSHAKE = 8 * 1024
_MAX_FILE_SIZE = 1 << 40
_DISK_MARGIN = 256 * 1024 * 1024
_ACK_OK = b"\x01"
_ACK_FAIL = b"\x00"
_REGISTRY_DIR = ".cache/inkhole/peers"
_REGISTRY_LIMIT = 8 * 1024
_LEASE_SECONDS = 75
_HEARTBEAT_SECONDS = 20
_POLL_SECONDS = 5
_MAX_PEERS = 256
_MAX_FRAME_WIRE = FRAME_PLAIN_LIMIT + 26
_COMMON_USERS = ("root", "ubuntu", "debian", "ec2-user", "admin")


@dataclass(frozen=True)
class SshHostKey:
    algorithm: str
    fingerprint: str


@dataclass
class SshRelayConfig:
    host: str
    username: str
    port: int
    host_key: SshHostKey
    ssh_key: object = field(repr=False)
    registry_key: bytearray = field(repr=False)
    device_id: str = ""
    identity_private: str = ""
    inbox: str = "received"
    peer_name: str = ""
    connect_timeout: float = 12.0
    transfer_timeout: float = 300.0

    # Compatibility with the shared settings surface.
    listen_port: int = 0
    secret: str = ""
    trusted_only: bool = False
    instance_id: str = ""

    def __post_init__(self) -> None:
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("SSH 服务器地址或端口无效")
        if not self.device_id:
            self.device_id = uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", self.device_id):
            raise ValueError("SSH 设备 ID 无效")
        if not self.identity_private:
            self.identity_private = DeviceIdentity.generate().private_b64()
        if not self.peer_name:
            self.peer_name = socket.gethostname()
        self.instance_id = self.device_id

    def profile(self) -> dict:
        return {
            "host": self.host,
            "username": self.username,
            "port": self.port,
            "host_key_algorithm": self.host_key.algorithm,
            "host_key_fingerprint": self.host_key.fingerprint,
            "device_id": self.device_id,
            "identity_private": self.identity_private,
        }

    def clear_credentials(self) -> None:
        self.registry_key[:] = b"\x00" * len(self.registry_key)
        self.ssh_key = None


class SshPeerInfo:
    __slots__ = ("name", "host", "port", "service_name", "hosts",
                 "device_id", "public_key")

    def __init__(self, name: str, host: str, port: int, device_id: str,
                 public_key: str):
        self.name = name
        self.host = host
        self.port = port
        self.service_name = device_id
        self.hosts = [host]
        self.device_id = device_id
        self.public_key = public_key

    @property
    def instance_id(self) -> str:
        return self.device_id[:8]

    def __str__(self) -> str:
        return f"{self.name} (SSH)"


def _paramiko():
    try:
        import paramiko
        return paramiko
    except ImportError as exc:
        raise RuntimeError("SSH 远程模式需要 paramiko 库") from exc


def _fingerprint(key) -> SshHostKey:
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest())
    return SshHostKey(key.get_name(), "SHA256:" + digest.decode("ascii").rstrip("="))


def probe_ssh_host_key(host: str, port: int = 22,
                       timeout: float = 10.0) -> SshHostKey:
    paramiko = _paramiko()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        transport = paramiko.Transport(raw)
        try:
            transport.start_client(timeout=timeout)
            return _fingerprint(transport.get_remote_server_key())
        finally:
            transport.close()


def _canonical_key_text(value: bytes) -> bytes:
    text = value.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip().encode("utf-8") + b"\n"


def load_ssh_private_key(private_key: bytearray,
                         passphrase: bytearray | None = None):
    """Parse an SSH key and derive a group key without retaining input text."""
    paramiko = _paramiko()
    normalized = _canonical_key_text(bytes(private_key))
    password = bytes(passphrase).decode("utf-8") if passphrase else None
    errors = []
    try:
        for key_class in (paramiko.RSAKey, paramiko.ECDSAKey,
                          paramiko.Ed25519Key):
            try:
                parsed = key_class.from_private_key(
                    io.StringIO(normalized.decode("utf-8")), password=password)
                seed = hashlib.sha256(normalized).digest()
                group_key = hmac.new(
                    seed, b"inkhole ssh relay registry v1", hashlib.sha256).digest()
                return parsed, bytearray(group_key)
            except Exception as exc:
                errors.append(exc)
        raise ValueError("无法读取 SSH 私钥，或私钥口令不正确") from errors[-1]
    finally:
        private_key[:] = b"\x00" * len(private_key)
        if passphrase is not None:
            passphrase[:] = b"\x00" * len(passphrase)


def _exact_host_key_policy(expected: SshHostKey):
    paramiko = _paramiko()

    class ExactHostKeyPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            actual = _fingerprint(key)
            if actual != expected:
                raise paramiko.SSHException(
                    f"SSH 主机密钥已变化：期望 {expected.fingerprint}，"
                    f"实际 {actual.fingerprint}")

    return ExactHostKeyPolicy()


def connect_ssh(host: str, port: int, username: str, ssh_key,
                host_key: SshHostKey, timeout: float = 12.0):
    """Authenticate and return ``(client, actual_username)``.

    An empty username tries common cloud image accounts so the basic UI only
    needs a host and private key.
    """
    paramiko = _paramiko()
    candidates = (username,) if username else _COMMON_USERS
    last_auth = None
    for candidate in dict.fromkeys(candidates):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_exact_host_key_policy(host_key))
        try:
            client.connect(
                host, port=port, username=candidate, pkey=ssh_key,
                look_for_keys=False, allow_agent=False, timeout=timeout,
                banner_timeout=timeout, auth_timeout=timeout,
            )
            transport = client.get_transport()
            if transport is None:
                raise ConnectionError("SSH 连接未建立")
            transport.set_keepalive(30)
            return client, candidate
        except paramiko.AuthenticationException as exc:
            last_auth = exc
            client.close()
            continue
        except Exception:
            client.close()
            raise
    raise PermissionError("SSH 密钥鉴权失败，请在高级设置中确认用户名") from last_auth


def validate_ssh_access(host: str, port: int, username: str, ssh_key,
                        host_key: SshHostKey) -> str:
    """Validate authentication, SFTP, and remote forwarding support."""
    client = None
    try:
        client, actual_user = connect_ssh(
            host, port, username, ssh_key, host_key)
        with client.open_sftp() as sftp:
            sftp.normalize(".")
        transport = client.get_transport()
        if transport is None:
            raise ConnectionError("SSH 连接未建立")
        remote_port = transport.request_port_forward("127.0.0.1", 0)
        try:
            if not remote_port:
                raise PermissionError("服务器未分配 SSH 反向转发端口")
        finally:
            if remote_port:
                transport.cancel_port_forward("127.0.0.1", remote_port)
        return actual_user
    except Exception as exc:
        text = str(exc).lower()
        if "administratively prohibited" in text or "port forwarding" in text:
            raise PermissionError(
                "服务器禁止 SSH TCP 转发，请启用 AllowTcpForwarding") from exc
        raise
    finally:
        if client:
            client.close()


def _packed(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("字段过长")
    return struct.pack("!H", len(raw)) + raw


def _registry_transcript(device_id: str, name: str, port: int,
                         public_key: str) -> bytes:
    return (b"inkhole-ssh-registry-v1\x00" + _packed(device_id) +
            _packed(name) + struct.pack("!H", port) + _packed(public_key))


def encode_registry_record(device_id: str, name: str, port: int,
                           public_key: str, registry_key: bytes) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{32}", device_id):
        raise ValueError("设备 ID 无效")
    if not 1 <= len(name) <= 80 or not 1 <= port <= 65535:
        raise ValueError("设备登记内容无效")
    transcript = _registry_transcript(device_id, name, port, public_key)
    signature = base64.urlsafe_b64encode(
        hmac.new(registry_key, transcript, hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    raw = json.dumps({
        "v": _PROTOCOL_VERSION,
        "id": device_id,
        "name": name,
        "port": port,
        "public_key": public_key,
        "mac": signature,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(raw) > _REGISTRY_LIMIT:
        raise ValueError("设备登记内容过大")
    return raw


def decode_registry_record(raw: bytes, registry_key: bytes) -> dict:
    if not raw or len(raw) > _REGISTRY_LIMIT:
        raise ValueError("设备登记内容大小无效")
    data = json.loads(raw.decode("utf-8"))
    if data.get("v") != _PROTOCOL_VERSION:
        raise ValueError("设备登记版本不支持")
    device_id = str(data.get("id", ""))
    name = str(data.get("name", ""))
    public_key = str(data.get("public_key", ""))
    port = data.get("port")
    if (not re.fullmatch(r"[0-9a-f]{32}", device_id) or
            not 1 <= len(name) <= 80 or isinstance(port, bool) or
            not isinstance(port, int) or not 1 <= port <= 65535 or
            not 40 <= len(public_key) <= 1024):
        raise ValueError("设备登记字段无效")
    expected = hmac.new(
        registry_key,
        _registry_transcript(device_id, name, port, public_key),
        hashlib.sha256,
    ).digest()
    try:
        actual = base64.urlsafe_b64decode(
            str(data.get("mac", "")) + "=" * (-len(str(data.get("mac", ""))) % 4))
    except Exception as exc:
        raise ValueError("设备登记签名无效") from exc
    if not hmac.compare_digest(expected, actual):
        raise ValueError("设备登记签名不匹配")
    return {"id": device_id, "name": name, "port": port,
            "public_key": public_key}


def _offer_transcript(transfer_id: str, sender_id: str, receiver_id: str,
                      public_key: str) -> bytes:
    return (b"inkhole-ssh-offer-v1\x00" + _packed(transfer_id) +
            _packed(sender_id) + _packed(receiver_id) + _packed(public_key))


def _encode_offer(transfer_id: str, sender_id: str, receiver_id: str,
                  public_key: str, registry_key: bytes) -> bytes:
    transcript = _offer_transcript(
        transfer_id, sender_id, receiver_id, public_key)
    signature = base64.urlsafe_b64encode(
        hmac.new(registry_key, transcript, hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return json.dumps({
        "v": _PROTOCOL_VERSION,
        "transfer_id": transfer_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "public_key": public_key,
        "mac": signature,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _decode_offer(raw: bytes, registry_key: bytes, receiver_id: str) -> dict:
    if not raw or len(raw) > _MAX_HANDSHAKE:
        raise ValueError("SSH 传输握手大小无效")
    data = json.loads(raw.decode("utf-8"))
    transfer_id = str(data.get("transfer_id", ""))
    sender_id = str(data.get("sender_id", ""))
    target_id = str(data.get("receiver_id", ""))
    public_key = str(data.get("public_key", ""))
    if (data.get("v") != _PROTOCOL_VERSION or target_id != receiver_id or
            not re.fullmatch(r"[0-9a-f-]{32,36}", transfer_id) or
            not re.fullmatch(r"[0-9a-f]{32}", sender_id) or
            not 40 <= len(public_key) <= 1024):
        raise ValueError("SSH 传输握手字段无效")
    expected = hmac.new(
        registry_key,
        _offer_transcript(transfer_id, sender_id, target_id, public_key),
        hashlib.sha256,
    ).digest()
    try:
        encoded = str(data.get("mac", ""))
        actual = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception as exc:
        raise ValueError("SSH 传输握手签名无效") from exc
    if not hmac.compare_digest(expected, actual):
        raise ValueError("SSH 传输握手签名不匹配")
    return {"transfer_id": transfer_id, "sender_id": sender_id,
            "receiver_id": target_id, "public_key": public_key}


def _recv_exact(channel, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = channel.recv(size - len(result))
        if not chunk:
            raise EOFError("SSH 数据通道已中断")
        result.extend(chunk)
    return bytes(result)


class _FrameStream:
    def __init__(self, channel, cipher: RelayCipher, timeout: float):
        self.channel = channel
        self.cipher = cipher
        self.channel.settimeout(timeout)

    def send(self, direction: int, plain: bytes) -> None:
        frame = self.cipher.seal(direction, plain)
        self.channel.sendall(struct.pack("!I", len(frame)) + frame)

    def receive(self, direction: int) -> bytes:
        size = struct.unpack("!I", _recv_exact(self.channel, 4))[0]
        if not 26 <= size <= _MAX_FRAME_WIRE:
            raise ValueError("SSH 加密帧长度无效")
        return self.cipher.open(_recv_exact(self.channel, size), direction)


class _FrameReader:
    def __init__(self, stream: _FrameStream, direction: int):
        self.stream = stream
        self.direction = direction
        self.buffer = bytearray()

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self.buffer.extend(self.stream.receive(self.direction))
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value


class SshRelayNode:
    """Remote transport backed only by a standard OpenSSH server."""

    def __init__(self, cfg: SshRelayConfig,
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
        self.on_progress = on_progress
        self._identity = DeviceIdentity.from_private_b64(cfg.identity_private)
        self._instance_id = cfg.device_id
        self._peers: dict[str, SshPeerInfo] = {}
        self._selected_peer: str | None = None
        self._running = False
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()
        self._registry_dirty = threading.Event()   # 登记内容变了(如改名),下轮立即重写
        self._thread: threading.Thread | None = None
        self._client = None
        self._transport = None
        self._remote_port = 0
        self._channels: set[object] = set()
        self._state_lock = threading.RLock()
        self._seen_transfers: dict[str, float] = {}
        os.makedirs(cfg.inbox, exist_ok=True)

    @property
    def actual_port(self) -> int:
        return self.cfg.port

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._connection_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        self._connected.clear()
        with self._state_lock:
            client, self._client = self._client, None
            channels = list(self._channels)
            self._channels.clear()
        for channel in channels:
            try:
                channel.close()
            except Exception:
                pass
        if client:
            try:
                client.close()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)
        with self._state_lock:
            self._transport = None
            self._remote_port = 0
            self._peers.clear()
            self._selected_peer = None
        if self.on_peers_changed:
            self.on_peers_changed()

    def _connection_loop(self) -> None:
        delay = 1.0
        while self._running:
            client = None
            sftp = None
            remote_port = 0
            try:
                self._status("正在连接 SSH 服务器")
                client, _ = connect_ssh(
                    self.cfg.host, self.cfg.port, self.cfg.username,
                    self.cfg.ssh_key, self.cfg.host_key,
                    self.cfg.connect_timeout)
                transport = client.get_transport()
                if transport is None:
                    raise ConnectionError("SSH 连接未建立")
                remote_port = transport.request_port_forward(
                    "127.0.0.1", 0, handler=self._accept_channel)
                if not remote_port:
                    raise PermissionError("服务器未分配 SSH 反向转发端口")
                sftp = client.open_sftp()
                self._ensure_registry(sftp)
                with self._state_lock:
                    self._client = client
                    self._transport = transport
                    self._remote_port = remote_port
                self._connected.set()
                self._write_registry(sftp, remote_port)
                self._poll_registry(sftp)
                self._status("SSH 远程通道已连接")
                delay = 1.0
                last_heartbeat = time.monotonic()
                while self._running and transport.is_active():
                    self._refresh_event.wait(_POLL_SECONDS)
                    self._refresh_event.clear()
                    now = time.monotonic()
                    if (self._registry_dirty.is_set() or
                            now - last_heartbeat >= _HEARTBEAT_SECONDS):
                        self._registry_dirty.clear()
                        self._write_registry(sftp, remote_port)
                        last_heartbeat = now
                    self._poll_registry(sftp)
                if self._running:
                    raise ConnectionError("SSH 连接已断开")
            except Exception as exc:
                if self._running:
                    message = str(exc)
                    if "administratively prohibited" in message.lower():
                        message = "服务器禁止 TCP 转发，请启用 AllowTcpForwarding"
                    self._status(f"SSH 远程通道断开：{message}")
            finally:
                self._connected.clear()
                if sftp:
                    try:
                        sftp.remove(f"{_REGISTRY_DIR}/{self.cfg.device_id}.json")
                    except Exception:
                        pass
                    try:
                        sftp.close()
                    except Exception:
                        pass
                if remote_port and client:
                    try:
                        transport = client.get_transport()
                        if transport:
                            transport.cancel_port_forward("127.0.0.1", remote_port)
                    except Exception:
                        pass
                if client:
                    try:
                        client.close()
                    except Exception:
                        pass
                with self._state_lock:
                    if self._client is client:
                        self._client = None
                        self._transport = None
                        self._remote_port = 0
                    self._peers.clear()
                    self._selected_peer = None
                if self.on_peers_changed:
                    self.on_peers_changed()
            if self._running and not self._stop_event.wait(delay):
                delay = min(delay * 2, 15.0)

    @staticmethod
    def _ensure_registry(sftp) -> None:
        current = ""
        for part in _REGISTRY_DIR.split("/"):
            current = f"{current}/{part}" if current else part
            try:
                sftp.mkdir(current, mode=0o700)
            except IOError:
                pass
            try:
                sftp.chmod(current, 0o700)
            except IOError:
                pass

    def _write_registry(self, sftp, remote_port: int) -> None:
        raw = encode_registry_record(
            self.cfg.device_id, self.cfg.peer_name, remote_port,
            self._identity.public_b64(), bytes(self.cfg.registry_key))
        target = f"{_REGISTRY_DIR}/{self.cfg.device_id}.json"
        temporary = f"{target}.{uuid.uuid4().hex[:8]}.tmp"
        with sftp.file(temporary, "wb") as output:
            output.write(raw)
            output.flush()
        sftp.chmod(temporary, 0o600)
        try:
            sftp.posix_rename(temporary, target)
        except Exception:
            try:
                sftp.remove(target)
            except IOError:
                pass
            sftp.rename(temporary, target)

    def _poll_registry(self, sftp) -> None:
        now = time.time()
        records = []
        for entry in sftp.listdir_attr(_REGISTRY_DIR)[:_MAX_PEERS + 32]:
            name = entry.filename
            if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
                continue
            path = f"{_REGISTRY_DIR}/{name}"
            if now - entry.st_mtime > _LEASE_SECONDS:
                try:
                    sftp.remove(path)
                except IOError:
                    pass
                continue
            if entry.st_size > _REGISTRY_LIMIT:
                continue
            try:
                with sftp.file(path, "rb") as source:
                    record = decode_registry_record(
                        source.read(_REGISTRY_LIMIT + 1),
                        bytes(self.cfg.registry_key))
                if record["id"] != self.cfg.device_id:
                    records.append(record)
            except (IOError, ValueError, json.JSONDecodeError, UnicodeError):
                continue
            if len(records) >= _MAX_PEERS:
                break
        counts: dict[str, int] = {}
        for item in records:
            counts[item["name"]] = counts.get(item["name"], 0) + 1
        updated: dict[str, SshPeerInfo] = {}
        for item in records:
            display = item["name"]
            if counts[display] > 1:
                display = f"{display} · {item['id'][:4]}"
            updated[display] = SshPeerInfo(
                display, self.cfg.host, item["port"], item["id"],
                item["public_key"])
        changed = False
        with self._state_lock:
            selected_id = None
            if self._selected_peer in self._peers:
                selected_id = self._peers[self._selected_peer].device_id
            old = {(p.device_id, p.name, p.port, p.public_key)
                   for p in self._peers.values()}
            new = {(p.device_id, p.name, p.port, p.public_key)
                   for p in updated.values()}
            changed = old != new
            self._peers = updated
            if selected_id:
                self._selected_peer = next(
                    (name for name, peer in updated.items()
                     if peer.device_id == selected_id), None)
        if changed and self.on_peers_changed:
            self.on_peers_changed()

    def _accept_channel(self, channel, _origin, _server) -> None:
        with self._state_lock:
            if not self._running:
                channel.close()
                return
            self._channels.add(channel)
        threading.Thread(
            target=self._receive_channel, args=(channel,), daemon=True).start()

    def peers(self) -> list[SshPeerInfo]:
        with self._state_lock:
            return sorted(self._peers.values(), key=lambda peer: peer.name)

    def selected_peer(self) -> str | None:
        with self._state_lock:
            return self._selected_peer

    def select_peer(self, name: str | None) -> None:
        with self._state_lock:
            self._selected_peer = name if name in self._peers else None

    def rename(self, name: str) -> None:
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("设备名称不能为空且不能超过 80 个字符")
        self.cfg.peer_name = name
        self._registry_dirty.set()   # 名字变了:让心跳循环下一轮立即重写登记
        self._refresh_event.set()

    def send_file(self, local_path: str) -> bool:
        if not os.path.isfile(local_path):
            self._status("发送失败：文件不存在")
            return False
        with self._state_lock:
            peer = self._peers.get(self._selected_peer or "")
            transport = self._transport
        if not peer:
            self._status("请先选择 SSH 远程目标设备")
            return False
        if not self._connected.is_set() or transport is None:
            self._status("SSH 远程通道尚未连接")
            return False
        transfer_id = str(uuid.uuid4())
        channel = None
        try:
            channel = transport.open_channel(
                "direct-tcpip", ("127.0.0.1", peer.port),
                ("127.0.0.1", 0), timeout=self.cfg.connect_timeout)
            channel.settimeout(self.cfg.transfer_timeout)
            with self._state_lock:
                self._channels.add(channel)
            offer = _encode_offer(
                transfer_id, self.cfg.device_id, peer.device_id,
                self._identity.public_b64(), bytes(self.cfg.registry_key))
            if len(offer) > _MAX_HANDSHAKE:
                raise ValueError("SSH 传输握手过大")
            channel.sendall(
                _HANDSHAKE_MAGIC + struct.pack("!I", len(offer)) + offer)
            if _recv_exact(channel, 1) != _ACK_OK:
                raise PermissionError("目标设备拒绝 SSH 传输握手")
            key = derive_transfer_key(
                self._identity, peer.public_key, transfer_id,
                self.cfg.device_id, peer.device_id)
            stream = _FrameStream(
                channel,
                RelayCipher(key, transfer_id, self.cfg.device_id, peer.device_id),
                self.cfg.transfer_timeout)
            filename = os.path.basename(local_path)
            size = os.path.getsize(local_path)
            header = json.dumps({
                "filename": filename,
                "size": size,
                "encrypted": True,
                "enc_mode": "ssh-aead",
                "want_ack": True,
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if not 0 < len(header) <= _MAX_HEADER:
                raise ValueError("文件名过长")
            stream.send(0, _MAGIC + struct.pack("!I", len(header)) + header)
            sent = 0
            # 与局域网传输一致的 0.25s 节流:64KB 一帧的裸回调会打爆 UI 线程
            progress = _Progress(self.on_progress, "send", filename, size)
            with open(local_path, "rb") as source:
                while True:
                    chunk = source.read(FRAME_PLAIN_LIMIT)
                    if not chunk:
                        break
                    stream.send(0, chunk)
                    sent += len(chunk)
                    progress.update(sent)
            if stream.receive(1) != _ACK_OK:
                raise IOError("目标设备未确认文件落盘")
            if self.on_sent:
                self.on_sent(filename)
            return True
        except Exception as exc:
            self._status(f"SSH 远程发送失败：{exc}")
            return False
        finally:
            self._close_channel(channel)

    def _receive_channel(self, channel) -> None:
        part_path = None
        stream = None
        ok = False
        try:
            channel.settimeout(self.cfg.transfer_timeout)
            if _recv_exact(channel, 4) != _HANDSHAKE_MAGIC:
                raise ValueError("SSH 传输握手标识无效")
            offer_size = struct.unpack("!I", _recv_exact(channel, 4))[0]
            if not 0 < offer_size <= _MAX_HANDSHAKE:
                raise ValueError("SSH 传输握手长度无效")
            offer = _decode_offer(
                _recv_exact(channel, offer_size), bytes(self.cfg.registry_key),
                self.cfg.device_id)
            now = time.monotonic()
            with self._state_lock:
                self._seen_transfers = {
                    key: created for key, created in self._seen_transfers.items()
                    if now - created < 600
                }
                if offer["transfer_id"] in self._seen_transfers:
                    raise ValueError("重复的 SSH 传输握手")
                self._seen_transfers[offer["transfer_id"]] = now
                known = next((peer for peer in self._peers.values()
                              if peer.device_id == offer["sender_id"]), None)
            if known and known.public_key != offer["public_key"]:
                raise PermissionError("发送设备公钥与在线登记不一致")
            channel.sendall(_ACK_OK)
            key = derive_transfer_key(
                self._identity, offer["public_key"], offer["transfer_id"],
                offer["sender_id"], self.cfg.device_id)
            stream = _FrameStream(
                channel,
                RelayCipher(key, offer["transfer_id"], offer["sender_id"],
                            self.cfg.device_id),
                self.cfg.transfer_timeout)
            reader = _FrameReader(stream, 0)
            if reader.read_exact(4) != _MAGIC:
                raise ValueError("WHPP magic 非法")
            header_len = struct.unpack("!I", reader.read_exact(4))[0]
            if not 0 < header_len <= _MAX_HEADER:
                raise ValueError("WHPP 头长度非法")
            header = json.loads(reader.read_exact(header_len).decode("utf-8"))
            filename = _safe_filename(str(header.get("filename", "")))
            size = header.get("size")
            if (isinstance(size, bool) or not isinstance(size, int) or
                    not 0 <= size <= _MAX_FILE_SIZE):
                raise ValueError("文件大小声明非法")
            os.makedirs(self.cfg.inbox, exist_ok=True)
            if size + _DISK_MARGIN > shutil.disk_usage(self.cfg.inbox).free:
                raise OSError("收件箱磁盘空间不足")
            destination = _unique_path(self.cfg.inbox, filename)
            part_path = destination + f".{uuid.uuid4().hex[:8]}.part"
            received = 0
            progress = _Progress(self.on_progress, "recv", filename, size)
            with open(part_path, "wb") as target:
                while received < size:
                    chunk = reader.read_exact(
                        min(FRAME_PLAIN_LIMIT, size - received))
                    target.write(chunk)
                    received += len(chunk)
                    progress.update(received)
                target.flush()
                os.fsync(target.fileno())
            destination = _unique_path(self.cfg.inbox, filename)
            os.replace(part_path, destination)
            part_path = None
            ok = True
            if self.on_received:
                self.on_received(destination)
        except Exception as exc:
            self._status(f"SSH 远程接收失败：{exc}")
        finally:
            if part_path:
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            if stream:
                try:
                    stream.send(1, _ACK_OK if ok else _ACK_FAIL)
                except Exception:
                    pass
            self._close_channel(channel)

    def _close_channel(self, channel) -> None:
        if channel is None:
            return
        with self._state_lock:
            self._channels.discard(channel)
        try:
            channel.close()
        except Exception:
            pass

    def _status(self, message: str) -> None:
        if self.on_status:
            self.on_status(message)
