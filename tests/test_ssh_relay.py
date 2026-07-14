import os
import socket
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from inkhole.relay_crypto import DeviceIdentity, RelayCipher
from inkhole.ssh_relay import (
    SshHostKey,
    SshPeerInfo,
    SshRelayConfig,
    SshRelayNode,
    _FrameStream,
    _decode_offer,
    _encode_offer,
    decode_registry_record,
    encode_registry_record,
    load_ssh_private_key,
)


def test_registry_record_is_authenticated():
    key = b"k" * 32
    device_id = "a" * 32
    identity = DeviceIdentity.generate()
    raw = encode_registry_record(
        device_id, "Desktop", 24567, identity.public_b64(), key)
    assert decode_registry_record(raw, key) == {
        "id": device_id,
        "name": "Desktop",
        "port": 24567,
        "public_key": identity.public_b64(),
    }

    tampered = raw.replace(b"Desktop", b"DesktoX")
    with pytest.raises(ValueError, match="签名"):
        decode_registry_record(tampered, key)
    with pytest.raises(ValueError, match="签名"):
        decode_registry_record(raw, b"x" * 32)


def test_offer_is_bound_to_receiver_and_group_key():
    key = b"g" * 32
    sender = "1" * 32
    receiver = "2" * 32
    transfer = "12345678-1234-1234-1234-123456789abc"
    public = DeviceIdentity.generate().public_b64()
    raw = _encode_offer(transfer, sender, receiver, public, key)
    offer = _decode_offer(raw, key, receiver)
    assert offer["sender_id"] == sender
    assert offer["receiver_id"] == receiver
    with pytest.raises(ValueError, match="字段"):
        _decode_offer(raw, key, "3" * 32)
    with pytest.raises(ValueError, match="签名"):
        _decode_offer(raw, b"z" * 32, receiver)


def test_ssh_frame_stream_preserves_boundaries():
    left, right = socket.socketpair()
    transfer = "12345678-1234-1234-1234-123456789abc"
    key = os.urandom(32)
    sender = _FrameStream(
        left, RelayCipher(key, transfer, "a" * 32, "b" * 32), 2)
    receiver = _FrameStream(
        right, RelayCipher(key, transfer, "a" * 32, "b" * 32), 2)
    try:
        sender.send(0, b"first")
        sender.send(0, b"second")
        assert receiver.receive(0) == b"first"
        assert receiver.receive(0) == b"second"
        receiver.send(1, b"\x01")
        assert sender.receive(1) == b"\x01"
    finally:
        left.close()
        right.close()


def test_private_key_input_is_cleared_and_group_key_is_stable():
    private = ed25519.Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    first = bytearray(raw.replace(b"\n", b"\r\n"))
    second = bytearray(raw)
    parsed_a, group_a = load_ssh_private_key(first)
    parsed_b, group_b = load_ssh_private_key(second)
    assert parsed_a is not None and parsed_b is not None
    assert group_a == group_b
    assert not any(first)
    assert not any(second)


class _LoopbackTransport:
    def __init__(self, receiver):
        self.receiver = receiver

    def open_channel(self, *_args, **_kwargs):
        sender, receiver = socket.socketpair()
        self.receiver._accept_channel(receiver, None, None)
        return sender


def _config(tmp_path, name, device_id, registry_key):
    identity = DeviceIdentity.generate()
    return SshRelayConfig(
        host="relay.example",
        username="root",
        port=22,
        host_key=SshHostKey("ssh-ed25519", "SHA256:test"),
        ssh_key=object(),
        registry_key=bytearray(registry_key),
        device_id=device_id,
        identity_private=identity.private_b64(),
        inbox=str(tmp_path),
        peer_name=name,
    )


def test_end_to_end_transfer_over_ssh_channel_contract(tmp_path):
    group_key = os.urandom(32)
    sender_cfg = _config(tmp_path / "sender", "Sender", "a" * 32, group_key)
    receiver_cfg = _config(tmp_path / "receiver", "Receiver", "b" * 32, group_key)
    received = []
    received_event = threading.Event()

    def on_received(path):
        received.append(path)
        received_event.set()

    receiver = SshRelayNode(receiver_cfg, on_received=on_received)
    sender = SshRelayNode(sender_cfg)
    receiver._running = True
    sender._running = True
    sender._connected.set()
    sender._transport = _LoopbackTransport(receiver)
    peer = SshPeerInfo(
        "Receiver", "relay.example", 25000, receiver_cfg.device_id,
        receiver._identity.public_b64())
    sender._peers = {peer.name: peer}
    sender.select_peer(peer.name)

    source = tmp_path / "payload.bin"
    source.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
    try:
        assert sender.send_file(str(source))
        assert received_event.wait(5)
        assert len(received) == 1
        assert open(received[0], "rb").read() == source.read_bytes()
    finally:
        sender._running = False
        receiver._running = False
        for channel in list(receiver._channels):
            channel.close()


def test_profile_never_contains_ssh_secret(tmp_path):
    cfg = _config(tmp_path, "Device", "c" * 32, b"q" * 32)
    profile = cfg.profile()
    assert "ssh_key" not in profile
    assert "registry_key" not in profile
    assert "passphrase" not in profile
