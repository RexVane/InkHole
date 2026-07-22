import ipaddress

from inkhole.p2p import (
    _CAP_VERSION,
    _LAN_DISCOVERY_MAGIC,
    _decode_lan_announcement,
    _encode_lan_announcement,
    _lan_broadcast_targets,
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
