"""跨网桥接端点上的 WHPC 探测(IKAT 令牌鉴权)。

模拟 transport-core streamBridge 的行为：本地端点先消费 "IKAT"+token，
错令牌直接断开；之后照常应答签名的 WHPC——发送端据此得知对端真实能力
(whe4 / folder-v1)，让一次性短码与 SSH 通道也能协商 WHE4。
"""
import json
import socket
import struct
import threading

import pytest

from inkhole.device_identity import DeviceIdentity, capability_message
from inkhole.p2p import _probe_peer

TOKEN = "bridge-token-123"
INSTANCE = "a1b2c3d4e5f6a7b8a1b2c3d4e5f6a7b8"
CAPS = ["folder-v1", "reliable-v3", "whe4"]


def _read_exact(conn: socket.socket, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _bridge_endpoint(identity: DeviceIdentity) -> tuple[socket.socket, int]:
    """单连接假桥接：IKAT 鉴权通过才应答 WHPC，否则静默断开。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(5)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            prefix = _read_exact(conn, 4 + len(TOKEN))
            if prefix != b"IKAT" + TOKEN.encode("ascii"):
                return
            if _read_exact(conn, 4) != b"WHPC":
                return
            nonce = _read_exact(conn, 32)
            if nonce is None:
                return
            signature = identity.sign(capability_message(
                nonce, INSTANCE, "跨网设备", 3, CAPS))
            body = json.dumps({
                "version": 3,
                "caps": CAPS,
                "instance_id": INSTANCE,
                "peer_name": "跨网设备",
                "public_key": identity.public_key,
                "signature": signature,
            }).encode("utf-8")
            conn.sendall(b"WHPC" + struct.pack("!I", len(body)) + body)

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_probe_negotiates_whe4_through_bridge_auth():
    identity = DeviceIdentity.generate()
    server, port = _bridge_endpoint(identity)
    try:
        result = _probe_peer("127.0.0.1", port, 3.0, INSTANCE,
                             endpoint_token=TOKEN)
    finally:
        server.close()
    assert "whe4" in result.capabilities
    assert "folder-v1" in result.capabilities
    assert result.fingerprint == identity.fingerprint
    assert result.instance_id == INSTANCE


def test_probe_with_wrong_token_fails_instead_of_lying():
    identity = DeviceIdentity.generate()
    server, port = _bridge_endpoint(identity)
    try:
        with pytest.raises(OSError):
            _probe_peer("127.0.0.1", port, 1.5, INSTANCE,
                        endpoint_token="wrong-token-xxxx")
    finally:
        server.close()
