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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
    """A manual peer shows up immediately and carries a real file transfer."""
    recv = _node(tmp_path, "R")
    recv.start()
    try:
        send = _node(tmp_path, "S", manual=[
            {"name": "目标机", "host": "127.0.0.1", "port": recv.actual_port}])
        send.start()
        try:
            assert any(p.name == "目标机" for p in send.peers())
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
        # 对端不在线:乐观注册的条目应在几轮探测后被剔除
        assert _wait(lambda: not send.peers(), timeout=8.0)
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
    """add/remove keep cfg.manual_peers and the live peer table in sync."""
    node = _node(tmp_path, "S")
    node.start()
    try:
        node.add_manual_peer("甲", "100.64.0.2", 52130)
        assert node.cfg.manual_peers == [
            {"name": "甲", "host": "100.64.0.2", "port": 52130}]
        assert any(p.name == "甲" for p in node.peers())
        # 同 host:port 重复添加 = 更新,不产生第二条
        node.add_manual_peer("甲二", "100.64.0.2", 52130)
        assert len(node.cfg.manual_peers) == 1
        node.remove_manual_peer("100.64.0.2", 52130)
        assert node.cfg.manual_peers == []
        assert not any("100.64.0.2" in p.hosts for p in node.peers())
    finally:
        node.stop()


def test_occupied_fixed_port_falls_back_and_reports_actual_port(tmp_path):
    """Match Android: an occupied fixed port must not prevent startup."""
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
        assert node.actual_port > 0
        assert node.actual_port != requested
        assert statuses[-1] == (
            f"墨洞已开启 · 端口 {requested} 被占用，当前端口 {node.actual_port}")
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
    assert statuses == ["墨洞未开启：监听端口启动失败"]
