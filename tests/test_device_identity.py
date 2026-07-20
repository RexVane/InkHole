import hashlib

from inkhole.device_identity import (DeviceIdentity, capability_message,
                                      public_fingerprint, receiver_message,
                                      transfer_message, verify)


def test_device_identity_round_trip_and_challenge_binding():
    identity = DeviceIdentity.generate()
    restored = DeviceIdentity.from_private_key(identity.export_private_key())
    message = capability_message(
        bytes(range(32)), "a" * 32, "Desktop", 3,
        ["folder-v1", "reliable-v3"])
    signature = restored.sign(message)

    assert restored.public_key == identity.public_key
    assert public_fingerprint(identity.public_key) == identity.fingerprint
    assert verify(identity.public_key, message, signature)
    assert not verify(identity.public_key, message + b"changed", signature)


def test_receiver_identity_message_binds_target_and_transfer():
    header = {
        "filename": "report.txt",
        "plain_size": 12,
        "transfer_id": "b" * 64,
        "sha256": "c" * 64,
        "kind": "file",
        "mtime_ms": 123,
        "encrypted": False,
        "sender_instance_id": "a" * 32,
    }
    identity = DeviceIdentity.generate()
    message = receiver_message(bytes(range(32)), header, 4, "d" * 32)
    signature = identity.sign(message)

    assert verify(identity.public_key, message, signature)
    assert not verify(identity.public_key, receiver_message(
        bytes(range(32)), header, 4, "e" * 32), signature)


def test_identity_message_vectors_match_android():
    nonce = bytes(range(32))
    instance_id = "0123456789abcdef0123456789abcdef"
    header = {
        "filename": "测试.txt",
        "plain_size": 123456789,
        "transfer_id": "a" * 64,
        "sha256": "b" * 64,
        "encrypted": True,
        "kind": "file",
        "mtime_ms": 1_700_000_000_000,
        "sender_instance_id": instance_id,
    }

    assert hashlib.sha256(capability_message(
        nonce, instance_id, "安卓", 3,
        ["folder-v1", "reliable-v3"])).hexdigest() == (
            "c74fb2982e30e6b84c7c187377638e20fef5df26a586564347c1a1bd8a9b82bb")
    assert hashlib.sha256(transfer_message(nonce, header, 4096)).hexdigest() == (
        "2350202421a349a8078d1d14d82b426078fb0b2335bac7174c29004c7df4a29f")
    assert hashlib.sha256(receiver_message(
        nonce, header, 4096, "f" * 32)).hexdigest() == (
            "0280143df64be4022b9f6c4bd4a9e8efb7b3207bfc598c37050abff2301942a1")
