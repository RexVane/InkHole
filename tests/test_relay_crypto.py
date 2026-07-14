import json
from pathlib import Path

import pytest

from inkhole.relay_crypto import DeviceIdentity, RelayCipher, derive_transfer_key


VECTOR = json.loads(
    (Path(__file__).parent / "vectors" / "relay_crypto_v1.json").read_text("utf-8"))


def _pair():
    sender = DeviceIdentity.from_private_b64(VECTOR["sender_private"])
    receiver = DeviceIdentity.from_private_b64(VECTOR["receiver_private"])
    sender_key = derive_transfer_key(
        sender, VECTOR["receiver_public"], VECTOR["transfer_id"],
        VECTOR["sender_id"], VECTOR["receiver_id"])
    receiver_key = derive_transfer_key(
        receiver, VECTOR["sender_public"], VECTOR["transfer_id"],
        VECTOR["sender_id"], VECTOR["receiver_id"])
    return sender_key, receiver_key


def test_shared_p256_hkdf_vector():
    sender_key, receiver_key = _pair()
    assert sender_key == receiver_key
    assert sender_key.hex() == VECTOR["key_hex"]


def test_shared_aes_gcm_frame_vector():
    key, _ = _pair()
    cipher = RelayCipher(
        key, VECTOR["transfer_id"], VECTOR["sender_id"], VECTOR["receiver_id"])
    assert cipher.seal(0, bytes.fromhex(VECTOR["direction0_plain_hex"])).hex() == VECTOR["direction0_frame_hex"]
    assert cipher.seal(1, bytes.fromhex(VECTOR["direction1_plain_hex"])).hex() == VECTOR["direction1_frame_hex"]


def test_tamper_replay_order_direction_and_wrong_device_rejected():
    sender_key, receiver_key = _pair()
    sender = RelayCipher(sender_key, VECTOR["transfer_id"], VECTOR["sender_id"], VECTOR["receiver_id"])
    receiver = RelayCipher(receiver_key, VECTOR["transfer_id"], VECTOR["sender_id"], VECTOR["receiver_id"])
    first = sender.seal(0, b"first")
    second = sender.seal(0, b"second")
    with pytest.raises(ValueError, match="out-of-order"):
        receiver.open(second, 0)
    assert receiver.open(first, 0) == b"first"
    with pytest.raises(ValueError, match="out-of-order"):
        receiver.open(first, 0)
    damaged = bytearray(second)
    damaged[-1] ^= 1
    with pytest.raises(Exception):
        receiver.open(bytes(damaged), 0)
    with pytest.raises(ValueError, match="direction"):
        RelayCipher(receiver_key, VECTOR["transfer_id"], VECTOR["sender_id"], VECTOR["receiver_id"]).open(first, 1)
    wrong = RelayCipher(receiver_key, VECTOR["transfer_id"], VECTOR["sender_id"], "wrong-device")
    with pytest.raises(Exception):
        wrong.open(first, 0)


def test_wrong_ack_is_not_accepted_as_success():
    key, _ = _pair()
    sender = RelayCipher(key, VECTOR["transfer_id"], VECTOR["sender_id"], VECTOR["receiver_id"])
    receiver = RelayCipher(key, VECTOR["transfer_id"], VECTOR["sender_id"], VECTOR["receiver_id"])
    ack = receiver.seal(1, b"\x00")
    assert sender.open(ack, 1) != b"\x01"
