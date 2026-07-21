import json
import os
import socket
import struct
import threading
import time
import hashlib

import pytest

from inkhole.p2p import (P2PConfig, P2PNode, PeerInfo,
                         _connect_peer_socket)
from inkhole.device_identity import DeviceIdentity, transfer_message
from inkhole.pet import (_normalize_cross_network,
                         _summarize_transfer_paths)


def _read_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _send_whpp(port, prefix, filename, payload):
    identity = DeviceIdentity.generate()
    instance_id = hashlib.sha256(identity.public_bytes).hexdigest()[:32]
    digest = hashlib.sha256(payload).hexdigest()
    header = json.dumps({
        "version": 3,
        "filename": filename,
        "plain_size": len(payload),
        "transfer_id": hashlib.sha256(
            (filename + digest).encode("utf-8")).hexdigest(),
        "sha256": digest,
        "encrypted": False,
        "want_ack": True,
        "kind": "file",
        "sender_instance_id": instance_id,
        "sender_public_key": identity.public_key,
    }, separators=(",", ":")).encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.sendall(prefix + b"WHPP" + struct.pack("!I", len(header)) +
                     header)
        try:
            if conn.recv(1) != b"\x02":
                return b""
            offset = struct.unpack("!Q", _read_exact(conn, 8))[0]
            nonce = _read_exact(conn, 32)
            _read_exact(conn, 32)  # receiver instance id
            public_size = struct.unpack("!H", _read_exact(conn, 2))[0]
            _read_exact(conn, public_size)
            signature_size = struct.unpack("!H", _read_exact(conn, 2))[0]
            _read_exact(conn, signature_size)
            signature = identity.sign(
                transfer_message(nonce, json.loads(header), offset)).encode("ascii")
            conn.sendall(struct.pack("!H", len(signature)) + signature)
            conn.sendall(struct.pack("!Q", len(payload) - offset) + payload[offset:])
            return conn.recv(1)
        except (ConnectionResetError, socket.timeout):
            return b""


def test_external_peer_lifecycle_and_validation(tmp_path):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), enable_mdns=False, peer_name="Mac"))

    display = node.upsert_external_peer(
        "session-1", "Android", "127.0.0.1", 23456,
        "wormhole", "endpoint-token", "a" * 32)
    peer = node.peers()[0]
    assert display == "Android"
    assert peer.transport == "wormhole"
    assert peer.endpoint_token == "endpoint-token"
    assert peer.instance_id == "a" * 32

    node.remove_external_peer("session-1", "wormhole")
    assert node.peers() == []
    with pytest.raises(ValueError, match="必须位于本机"):
        node.upsert_external_peer(
            "session-2", "Remote", "192.0.2.1", 23456,
            "ssh", "endpoint-token")


def test_lan_probe_does_not_remove_transport_core_peers(tmp_path):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), enable_mdns=False, peer_name="Mac"))
    node._probe_interval = 0.01
    node._probe_strikes = 1
    node.upsert_external_peer(
        "android-id", "Android", "127.0.0.1", 23456,
        "ssh", "endpoint-token", "a" * 32)

    node.start()
    try:
        time.sleep(0.08)
        assert [peer.name for peer in node.peers()] == ["Android"]
    finally:
        node.stop()


def test_external_endpoint_capability_token_is_sent_first():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = []

    def serve():
        conn, _addr = listener.accept()
        with conn:
            received.append(_read_exact(conn, len(b"IKATtokenpayload")))
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    peer = PeerInfo(
        "Tunnel", "127.0.0.1", listener.getsockname()[1],
        endpoint_token="token")
    with _connect_peer_socket(peer, "127.0.0.1", 2) as conn:
        conn.sendall(b"payload")
    thread.join(2)

    assert received == [b"IKATtokenpayload"]


def test_transfer_falls_back_to_paired_ssh_route_for_same_device(
        tmp_path, monkeypatch):
    instance_id = "a" * 32
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), enable_mdns=False, peer_name="Mac"))
    node._on_peer_added(
        "Android", "100.64.0.2", 34505,
        service_name="manual|100.64.0.2|34505",
        instance_id=instance_id, manual=True, transport="tailscale")
    node.upsert_external_peer(
        instance_id, "Android", "127.0.0.1", 24000,
        "ssh", "endpoint-token", instance_id)
    direct = next(peer for peer in node.peers()
                  if peer.transport == "tailscale")
    calls = []
    expected = object()

    def connect(peer, _host, _timeout):
        calls.append(peer.transport)
        if peer.transport == "tailscale":
            raise OSError("direct route unavailable")
        return expected

    monkeypatch.setattr("inkhole.p2p._connect_peer_socket", connect)

    assert node._connect_for_transfer(direct) is expected
    assert calls == ["tailscale", "ssh"]

    calls.clear()
    assert node._connect_for_transfer(direct, route_offset=1) is expected
    assert calls == ["ssh"]


def test_authenticated_core_ingress_can_deliver_whpp(tmp_path):
    token = "runtime-only-token"
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), enable_mdns=False, peer_name="Receiver",
        trusted_only=True, core_ingress_token=token))
    node.start()
    try:
        bad_ack = _send_whpp(
            node.actual_port, b"IKCI" + b"x" * len(token),
            "rejected.txt", b"bad")
        assert bad_ack != b"\x01"
        assert not (tmp_path / "rejected.txt").exists()

        ack = _send_whpp(
            node.actual_port, b"IKCI" + token.encode("ascii"),
            "accepted.txt", b"through-core")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (tmp_path / "accepted.txt").exists():
            time.sleep(0.01)

        assert ack == b"\x01"
        assert (tmp_path / "accepted.txt").read_bytes() == b"through-core"
    finally:
        node.stop()


def test_cross_network_config_normalizes_untrusted_values():
    normalized = _normalize_cross_network({
        "wormhole": {"rendezvous_url": "  ws://mailbox  "},
        "ssh": {
            "enabled": True,
            "remote_port": "not-a-port",
            "profile": {"port": 70000, "private_key_mode": "unknown"},
            "peers": [
                {"instance_id": "ok", "remote_port": "22001",
                 "noise_public": "public", "name": "Phone"},
                {"instance_id": "bad", "remote_port": "broken",
                 "noise_public": "public"},
            ],
        },
    })

    assert normalized["wormhole"]["rendezvous_url"] == "ws://mailbox"
    assert normalized["ssh"]["profile"]["port"] == 22
    assert normalized["ssh"]["profile"]["private_key_mode"] == "file"
    assert normalized["ssh"]["remote_port"] == 0
    assert [peer["name"] for peer in normalized["ssh"]["peers"]] == ["Phone"]


def test_one_time_summary_counts_files_and_ignores_missing_paths(tmp_path):
    root = tmp_path / "folder"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "one.bin").write_bytes(b"123")
    (nested / "two.bin").write_bytes(b"4567")
    separate = tmp_path / "standalone.bin"
    separate.write_bytes(b"89")

    result = _summarize_transfer_paths([
        str(root), str(tmp_path / "missing"), str(separate)])

    assert result["paths"] == [os.path.abspath(root), os.path.abspath(separate)]
    assert result["summary"] == {
        "device_name": "",
        "instance_id": "",
        "item_count": 2,
        "file_count": 3,
        "directory_count": 2,
        "total_bytes": 9,
        "names": ["folder", "standalone.bin"],
    }


def test_one_time_summary_rejects_empty_selection(tmp_path):
    with pytest.raises(ValueError, match="没有可发送"):
        _summarize_transfer_paths([str(tmp_path / "missing")])
