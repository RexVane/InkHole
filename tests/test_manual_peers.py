"""Manual peer (Tailscale / fixed-IP direct connect) behavior tests.

mDNS multicast does not cross virtual overlay networks, so manually added
peers must: register at start, survive as send targets, drop out when the
remote goes offline, and come back automatically once it is reachable again.
All tests run mDNS-free on loopback.
"""

import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inkhole.p2p as p2p_mod
from inkhole.p2p import P2PConfig, P2PNode


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _wait(predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _node(tmp_path, name, port=0, manual=None, **probe):
    cfg = P2PConfig(inbox=str(tmp_path / f"{name}_inbox"), listen_port=port,
                    peer_name=name, enable_mdns=False,
                    manual_peers=list(manual or []))
    node = P2PNode(cfg)
    for key, value in probe.items():   # 探测参数提速(默认 5s 周期太慢)
        setattr(node, f"_probe_{key}", value)
    return node


def test_manual_peer_registers_and_transfers(tmp_path):
    """A manual peer appears after WHPC verification and transfers a file."""
    recv = _node(tmp_path, "R")
    recv.start()
    try:
        send = _node(tmp_path, "S", manual=[
            {"name": "目标机", "host": "127.0.0.1", "port": recv.actual_port}])
        send.start()
        try:
            assert _wait(lambda: any(p.name == "目标机" for p in send.peers()))
            entry = send.cfg.manual_peers[0]
            assert len(entry["instance_id"]) == 32
            assert entry["instance_id"] == recv.cfg.instance_id
            send.select_peer("目标机")
            payload = tmp_path / "hello.txt"
            payload.write_text("tailscale direct", encoding="utf-8")
            assert send.send_file(str(payload))
            inbox = tmp_path / "R_inbox"
            assert _wait(lambda: any(f.name == "hello.txt"
                                     for f in inbox.iterdir()))
            assert (inbox / "hello.txt").read_text(
                encoding="utf-8") == "tailscale direct"
        finally:
            send.stop()
    finally:
        recv.stop()


def test_manual_peer_readded_after_restart(tmp_path):
    """Offline manual peers are pruned, then auto-readded once reachable."""
    port = _free_port()
    send = _node(tmp_path, "S", manual=[
        {"name": "目标机", "host": "127.0.0.1", "port": port}],
        interval=0.2, timeout=0.3, strikes=2)
    send.start()
    try:
        # 对端不在线时绝不乐观显示。
        assert not send.peers()
        # 对端上线(绑同一端口):探测线程应自动加回来
        recv = _node(tmp_path, "R", port=port)
        recv.start()
        try:
            assert _wait(lambda: any(p.name == "目标机"
                                     for p in send.peers()), timeout=8.0)
        finally:
            recv.stop()
    finally:
        send.stop()


def test_manual_peer_add_remove_updates_config(tmp_path):
    """Unverified manual entries stay configured but never appear online."""
    node = _node(tmp_path, "S")
    node.start()
    try:
        node.add_manual_peer("甲", "100.64.0.2", 52130)
        assert node.cfg.manual_peers == [
            {"name": "甲", "host": "100.64.0.2", "port": 52130}]
        assert not node.peers()
        # 同 host:port 重复添加 = 更新,不产生第二条
        node.add_manual_peer("甲二", "100.64.0.2", 52130)
        assert len(node.cfg.manual_peers) == 1
        node.remove_manual_peer("100.64.0.2", 52130)
        assert node.cfg.manual_peers == []
        assert not any("100.64.0.2" in p.hosts for p in node.peers())
    finally:
        node.stop()


def test_occupied_fixed_port_stops_node_and_reports_error(tmp_path):
    """A configured cross-network port must never silently become random."""
    blocker = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("", 0))
    blocker.listen(1)
    requested = blocker.getsockname()[1]
    statuses = []
    node = P2PNode(
        P2PConfig(inbox=str(tmp_path / "fallback_inbox"),
                  listen_port=requested, peer_name="Fallback",
                  enable_mdns=False),
        on_status=statuses.append,
    )
    try:
        node.start()
        assert node.actual_port == 0
        assert not node._running
        assert statuses[-1].startswith(
            f"墨洞未开启：固定监听端口 {requested} 不可用:")
    finally:
        node.stop()
        blocker.close()


def test_listener_start_failure_is_reported_without_crashing(tmp_path, monkeypatch):
    statuses = []
    node = _node(tmp_path, "Failure")

    def fail_start():
        raise OSError("no sockets")

    monkeypatch.setattr(node, "_start_tcp_server", fail_start)
    node.on_status = statuses.append
    node.start()
    assert node.actual_port == 0
    assert not node._running
    assert statuses == ["墨洞未开启：监听端口启动失败: no sockets"]


def test_manual_peer_identity_change_requires_readding(tmp_path):
    """A pinned host:port cannot silently become a different InkHole node."""
    port = _free_port()
    recv_a = _node(tmp_path, "A", port=port)
    recv_a.start()
    send = _node(tmp_path, "S", manual=[
        {"name": "目标机", "host": "127.0.0.1", "port": port}],
        interval=0.1, timeout=0.2, strikes=1)
    statuses = []
    send.on_status = statuses.append
    send.start()
    try:
        assert _wait(lambda: bool(send.peers()))
        pinned = send.cfg.manual_peers[0]["instance_id"]
        assert pinned == recv_a.cfg.instance_id
        recv_a.stop()
        assert _wait(lambda: not send.peers())

        recv_b = _node(tmp_path, "B", port=port)
        recv_b.start()
        try:
            assert _wait(lambda: any("身份验证失败" in msg for msg in statuses))
            assert not send.peers()
            assert send.cfg.manual_peers[0]["instance_id"] == pinned
            assert "身份验证失败" in statuses[-1]
        finally:
            recv_b.stop()
    finally:
        send.stop()
        recv_a.stop()


def test_tailnet_ranges_and_magicdns_never_fall_back_to_default_route(monkeypatch):
    assert p2p_mod._is_tailnet_ip("100.64.0.1")
    assert p2p_mod._is_tailnet_ip("100.127.255.255")
    assert not p2p_mod._is_tailnet_ip("100.128.0.1")
    assert p2p_mod._is_tailnet_ip("fd7a:115c:a1e0::1")
    assert not p2p_mod._is_tailnet_ip("2001:db8::1")

    endpoint = p2p_mod._ResolvedEndpoint(
        socket.AF_INET, socket.SOCK_STREAM, 0,
        ("100.64.12.34", 41300), "100.64.12.34")
    monkeypatch.setattr(
        p2p_mod, "_resolve_endpoints", lambda host, port: [endpoint])
    monkeypatch.setattr(p2p_mod, "_tailnet_source_ip", lambda family: None)

    connected = False

    class FakeSocket:
        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, target):
            nonlocal connected
            connected = True

        def close(self):
            pass

    monkeypatch.setattr(p2p_mod.socket, "socket", lambda *args: FakeSocket())

    with pytest.raises(p2p_mod._TailnetUnavailable):
        p2p_mod._connect_transfer_socket(
            "workstation.example.ts.net", 41300, 0.1)
    assert not connected


def test_visible_manual_tailnet_peer_is_removed_on_first_route_failure(
        tmp_path, monkeypatch):
    identity = "0123456789abcdef0123456789abcdef"
    entry = {"name": "目标机", "host": "100.64.12.34", "port": 41300,
             "instance_id": identity}
    node = _node(tmp_path, "S", manual=[entry], interval=10, strikes=99)
    node._on_peer_added(
        "目标机", entry["host"], entry["port"],
        service_name=node._manual_key(entry), hosts=[entry["host"]],
        instance_id=identity, capabilities={"folder-v1"}, manual=True)

    def unavailable(*_args, **_kwargs):
        raise p2p_mod._TailnetUnavailable("Tailscale 接口不在线")

    monkeypatch.setattr(p2p_mod, "_probe_peer", unavailable)
    node.start()
    try:
        assert _wait(lambda: not node.peers(), timeout=1.0)
    finally:
        node.stop()
