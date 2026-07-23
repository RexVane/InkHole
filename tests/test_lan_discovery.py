import ipaddress
import time

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
    assert _decode_lan_announcement(
        _encode_lan_announcement(instance_id, 41300)
    ) == (instance_id, 41300, False)


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
