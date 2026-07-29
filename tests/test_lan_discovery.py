import errno
import ipaddress
import socket
import sys
import threading
import time
import types

import inkhole.p2p as p2p_module
import pytest

from inkhole.p2p import (
    _CAP_VERSION,
    _LAN_DISCOVERY_MAGIC,
    _decode_lan_announcement,
    _decode_lan_hint,
    _encode_lan_announcement,
    _encode_lan_hint,
    _lan_broadcast_targets,
    P2PConfig,
    P2PNode,
)


def test_lan_announcement_round_trip():
    instance_id = "0123456789abcdef0123456789abcdef"
    encoded = _encode_lan_announcement(instance_id, 41300)
    assert b'"bye"' not in encoded
    assert _decode_lan_announcement(encoded) == (
        instance_id, 41300, False, False)


def test_lan_announcement_rejects_bad_metadata():
    assert _decode_lan_announcement(b"{}") is None
    payload = (
        '{"magic":"%s","version":%d,"instance_id":"%s","port":41300}'
        % (_LAN_DISCOVERY_MAGIC, _CAP_VERSION - 1,
           "0123456789abcdef0123456789abcdef")
    ).encode("ascii")
    assert _decode_lan_announcement(payload) is None


def test_lan_broadcast_targets_include_directed_hotspot_broadcast():
    assert _lan_broadcast_targets([
        ipaddress.ip_network("10.237.115.0/24"),
        ipaddress.ip_network("192.168.7.8/29"),
    ]) == ["255.255.255.255", "10.237.115.255", "192.168.7.15"]


def test_local_ips_ignore_loopback_default_when_lan_address_exists(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))

    class Address:
        family = socket.AF_INET
        address = "192.168.50.7"

    monkeypatch.setattr(node, "_get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setitem(
        sys.modules, "psutil",
        types.SimpleNamespace(net_if_addrs=lambda: {"en0": [Address()]}),
    )

    assert node._get_local_ips() == ["192.168.50.7"]


def test_goodbye_definite_refusal_removes_current_peer_and_notifies(
        tmp_path, monkeypatch):
    changed = []
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False),
        on_peers_changed=lambda: changed.append(True))
    node._on_peer_added(
        "Android", "192.0.2.9", 41300,
        service_name="Android-test", hosts=["192.0.2.9"],
        instance_id="a" * 32, transport="lan")
    node.select_peer("Android")
    changed.clear()
    probed_ports = []

    def refused(_hosts, port, _timeout, _instance_id):
        probed_ports.append(port)
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(node, "_probe_hosts", refused)
    node._handle_lan_goodbye("192.0.2.9", 1, "a" * 32)

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and node.peer_names():
        time.sleep(0.01)

    assert probed_ports == [41300]
    assert node.peer_names() == []
    assert node.selected_peer() is None
    assert changed == [True]


def test_goodbye_timeout_keeps_peer(tmp_path, monkeypatch):
    changed = []
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False),
        on_peers_changed=lambda: changed.append(True))
    node._on_peer_added(
        "Android", "192.0.2.9", 41300,
        service_name="Android-test", hosts=["192.0.2.9"],
        instance_id="a" * 32, transport="lan")
    changed.clear()

    def timeout(*_args, **_kwargs):
        raise TimeoutError("sleeping phone")

    monkeypatch.setattr(node, "_probe_hosts", timeout)
    node._handle_lan_goodbye("192.0.2.9", 41300, "a" * 32)

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with node._lock:
            if not node._pending_goodbye_probes:
                break
        time.sleep(0.01)

    assert node.peer_names() == ["Android"]
    assert changed == []


def test_goodbye_does_not_remove_replacement_peer(tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))
    node._on_peer_added(
        "Android", "192.0.2.9", 41300,
        service_name="Android-test", hosts=["192.0.2.9"],
        instance_id="a" * 32, transport="lan")

    def replaced_then_refused(*_args, **_kwargs):
        node._on_peer_added(
            "Android", "192.0.2.10", 41300,
            service_name="replacement", hosts=["192.0.2.10"],
            instance_id="b" * 32, transport="lan")
        raise ConnectionRefusedError(errno.ECONNREFUSED, "old peer left")

    monkeypatch.setattr(node, "_probe_hosts", replaced_then_refused)
    node._handle_lan_goodbye("192.0.2.9", 41300, "a" * 32)

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with node._lock:
            if not node._pending_goodbye_probes:
                break
        time.sleep(0.01)

    assert len(node.peers()) == 1
    assert node.peers()[0].instance_id == "b" * 32


def test_goodbye_does_not_remove_reconnected_same_instance(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))
    instance_id = "a" * 32
    node._on_peer_added(
        "Android", "192.0.2.9", 41300,
        service_name="Android-test", hosts=["192.0.2.9"],
        instance_id=instance_id, transport="lan")

    def reconnected_then_refused(*_args, **_kwargs):
        node._on_peer_added(
            "Android", "192.0.2.10", 41400,
            service_name="Android-test", hosts=["192.0.2.10"],
            instance_id=instance_id, transport="lan")
        raise ConnectionRefusedError(errno.ECONNREFUSED, "old endpoint left")

    monkeypatch.setattr(node, "_probe_hosts", reconnected_then_refused)
    node._handle_lan_goodbye("192.0.2.9", 41300, instance_id)

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with node._lock:
            if not node._pending_goodbye_probes:
                break
        time.sleep(0.01)
    peers = node.peers()
    assert len(peers) == 1
    assert peers[0].instance_id == instance_id
    assert (peers[0].host, peers[0].port) == ("192.0.2.10", 41400)


def test_probe_failure_does_not_remove_reconnected_same_instance(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))
    instance_id = "a" * 32
    node._on_peer_added(
        "Android", "192.0.2.9", 41300,
        service_name="Android-test", hosts=["192.0.2.9"],
        instance_id=instance_id, transport="lan")
    node._running = True

    def reconnected_then_refused(*_args, **_kwargs):
        node._on_peer_added(
            "Android", "192.0.2.10", 41400,
            service_name="Android-test", hosts=["192.0.2.10"],
            instance_id=instance_id, transport="lan")
        node._running = False
        node._probe_wake.set()
        raise ConnectionRefusedError(errno.ECONNREFUSED, "old endpoint left")

    monkeypatch.setattr(node, "_probe_hosts", reconnected_then_refused)
    node._probe_loop()

    peers = node.peers()
    assert len(peers) == 1
    assert (peers[0].host, peers[0].port) == ("192.0.2.10", 41400)


def test_probe_hosts_keeps_timeout_classification_regardless_of_order(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))

    for errors in (
        [TimeoutError("timeout"),
         ConnectionRefusedError(errno.ECONNREFUSED, "refused")],
        [ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
         TimeoutError("timeout")],
    ):
        remaining = iter(errors)

        def fail(*_args, **_kwargs):
            raise next(remaining)

        monkeypatch.setattr(p2p_module, "_probe_peer", fail)
        with pytest.raises(TimeoutError):
            node._probe_hosts(["192.0.2.1", "192.0.2.2"], 41300, 0.01)


def test_probe_hosts_accepts_valid_address_after_identity_mismatch(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))
    expected = p2p_module._ProbeResult(
        "a" * 32, "Android", frozenset({"folder-v1"}),
        "192.0.2.2", "public-key", "fingerprint")

    def probe(host, *_args, **_kwargs):
        if host == "192.0.2.1":
            raise p2p_module._IdentityMismatch("stale address")
        return expected

    monkeypatch.setattr(p2p_module, "_probe_peer", probe)
    assert node._probe_hosts(
        ["192.0.2.1", "192.0.2.2"], 41300, 0.01, "a" * 32) is expected


def test_probe_hosts_treats_mismatch_plus_timeout_as_ambiguous(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))

    def probe(host, *_args, **_kwargs):
        if host == "192.0.2.1":
            raise p2p_module._IdentityMismatch("stale address")
        raise TimeoutError("expected device may be sleeping")

    monkeypatch.setattr(p2p_module, "_probe_peer", probe)
    with pytest.raises(TimeoutError):
        node._probe_hosts(
            ["192.0.2.1", "192.0.2.2"], 41300, 0.01, "a" * 32)


def test_unchanged_probe_keeps_peer_generation(tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))
    instance_id = "a" * 32
    node._on_peer_added(
        "Android", "192.0.2.9", 41300,
        service_name="Android-test", hosts=["192.0.2.9"],
        instance_id=instance_id, capabilities={"folder-v1"}, transport="lan")
    original = node.peers()[0]
    node._running = True

    def alive(*_args, **_kwargs):
        node._running = False
        node._probe_wake.set()
        return p2p_module._ProbeResult(
            instance_id, "Android", frozenset({"folder-v1"}),
            "192.0.2.9", "", "")

    monkeypatch.setattr(node, "_probe_hosts", alive)
    node._probe_loop()
    assert node.peers()[0] is original


def test_probe_requires_four_failures_without_rebuilding_mdns(
        tmp_path, monkeypatch):
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Mac", enable_mdns=False))
    node._probe_interval = 0.01
    node._probe_timeout = 0.01
    attempts = 0
    fourth_started = threading.Event()
    release_fourth = threading.Event()
    rebuilds = []

    def unavailable(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 4:
            fourth_started.set()
            assert release_fourth.wait(1)
        raise OSError("offline")

    monkeypatch.setattr(node, "_probe_hosts", unavailable)
    monkeypatch.setattr(node, "_rebuild_mdns", lambda reason: rebuilds.append(reason))
    node._on_peer_added(
        "Android", "192.168.50.8", 41300,
        service_name="Android-test._inkhole._tcp.local.",
        hosts=["192.168.50.8"],
        instance_id="a" * 32,
    )
    node.start()
    try:
        assert fourth_started.wait(1)
        assert node.peer_names() == ["Android"]
        release_fourth.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and node.peer_names():
            time.sleep(0.01)
        assert node.peer_names() == []
        assert attempts == 4
        assert rebuilds == []
        assert p2p_module._PROBE_STRIKES == 4
    finally:
        release_fourth.set()
        node.stop()


def test_reverse_lan_hint_round_trip_and_rejects_wrong_version():
    instance_id = "0123456789abcdef0123456789abcdef"
    frame = _encode_lan_hint(instance_id, 41300)
    assert len(frame) == 39
    assert _decode_lan_hint(frame) == (instance_id, 41300)
    assert _decode_lan_hint(frame[:4] + b"\x02" + frame[5:]) is None


def test_reverse_lan_hint_triggers_signed_callback_probe(tmp_path):
    sender = P2PNode(P2PConfig(
        inbox=str(tmp_path / "sender"),
        peer_name="Hint sender",
        enable_mdns=False,
    ))
    receiver = P2PNode(P2PConfig(
        inbox=str(tmp_path / "receiver"),
        peer_name="Hint receiver",
        enable_mdns=False,
    ))
    sender._running = True
    receiver._running = True
    sender._start_tcp_server()
    receiver._start_tcp_server()
    # The production path uses a physical LAN address. Loopback keeps this test
    # deterministic while exercising the same TCP frame and signed WHPC callback.
    receiver._hosts_on_current_lan = lambda hosts: hosts
    try:
        sender._send_lan_hint("127.0.0.1", receiver.actual_port)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "Hint sender" not in receiver.peer_names():
            time.sleep(0.02)
        peer = next((item for item in receiver.peers()
                     if item.name == "Hint sender"), None)
        assert peer is not None
        assert peer.instance_id == sender._instance_id
        assert peer.transport == "lan"
    finally:
        sender.stop()
        receiver.stop()


def test_verified_peer_name_replaces_stale_mdns_address(tmp_path):
    sender = P2PNode(P2PConfig(
        inbox=str(tmp_path / "sender"),
        peer_name="V2419A",
        enable_mdns=False,
    ))
    receiver = P2PNode(P2PConfig(
        inbox=str(tmp_path / "receiver"),
        peer_name="Mac",
        enable_mdns=False,
    ))
    sender._running = True
    receiver._running = True
    sender._start_tcp_server()
    receiver._start_tcp_server()
    receiver._hosts_on_current_lan = lambda hosts: hosts
    service_name = "V2419A-df5e129b._inkhole._tcp.local."
    stale_name = "10.230.74.167"
    receiver._on_peer_added(
        stale_name,
        "127.0.0.1",
        sender.actual_port,
        service_name=service_name,
        instance_id=sender._instance_id,
    )
    receiver.select_peer(stale_name)

    try:
        receiver._verify_discovered_peer(
            stale_name,
            ["127.0.0.1"],
            sender.actual_port,
            service_name,
            sender._instance_id,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and receiver.peer_names() != ["V2419A"]:
            time.sleep(0.02)

        assert receiver.peer_names() == ["V2419A"]
        assert receiver.selected_peer() == "V2419A"
        assert receiver.peers()[0].instance_id == sender._instance_id
    finally:
        sender.stop()
        receiver.stop()
