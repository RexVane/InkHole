"""End-to-end cryptography for SSH remote transfers.

The relay never receives a transfer key. Devices use persistent P-256
identities and derive a fresh AES-256-GCM key for every transfer.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_VERSION = 1
FRAME_PLAIN_LIMIT = 64 * 1024


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("empty base64 value")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("invalid base64 value") from exc


def _packed(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("identity field too long")
    return struct.pack("!H", len(raw)) + raw


@dataclass(frozen=True)
class DeviceIdentity:
    """Serializable local device identity. The private value stays client-side."""

    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> "DeviceIdentity":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_b64(cls, value: str) -> "DeviceIdentity":
        key = serialization.load_der_private_key(_b64d(value), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
                key.curve, ec.SECP256R1):
            raise ValueError("identity must be a P-256 private key")
        return cls(key)

    def private_b64(self) -> str:
        raw = self.private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return _b64e(raw)

    def public_b64(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return _b64e(raw)


def load_public_key(value: str) -> ec.EllipticCurvePublicKey:
    key = serialization.load_der_public_key(_b64d(value))
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1):
        raise ValueError("peer identity must be a P-256 public key")
    return key


def derive_transfer_key(identity: DeviceIdentity, peer_public_b64: str,
                        transfer_id: str, sender_id: str,
                        receiver_id: str) -> bytes:
    """Derive the cross-platform transfer key.

    IDs are length-prefixed to make the transcript unambiguous. Sender and
    receiver order is fixed by the offer, so both clients derive identical
    bytes while a reflected/wrong-device transcript does not.
    """
    shared = identity.private_key.exchange(ec.ECDH(), load_public_key(peer_public_b64))
    transcript = (b"inkhole-relay-v1\x00" + _packed(transfer_id) +
                  _packed(sender_id) + _packed(receiver_id))
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(transcript).digest(),
        info=b"inkhole relay transfer key\x00" + transcript,
    ).derive(shared)


class RelayCipher:
    """Strict ordered AEAD framing for one bidirectional SSH channel."""

    def __init__(self, key: bytes, transfer_id: str, sender_id: str,
                 receiver_id: str):
        if len(key) != 32:
            raise ValueError("AES-256 requires a 32-byte key")
        self._aead = AESGCM(key)
        self._aad_prefix = (b"IRF1" + _packed(transfer_id) +
                            _packed(sender_id) + _packed(receiver_id))
        self._send_seq = [0, 0]
        self._recv_seq = [0, 0]

    def seal(self, direction: int, plain: bytes) -> bytes:
        if direction not in (0, 1):
            raise ValueError("invalid frame direction")
        if len(plain) > FRAME_PLAIN_LIMIT:
            raise ValueError("relay frame exceeds 64 KiB")
        seq = self._send_seq[direction]
        if seq >= (1 << 64) - 1:
            raise OverflowError("relay frame sequence exhausted")
        header = struct.pack("!BBQ", PROTOCOL_VERSION, direction, seq)
        nonce = b"IRF" + bytes((direction,)) + struct.pack("!Q", seq)
        ciphertext = self._aead.encrypt(nonce, bytes(plain), self._aad_prefix + header)
        self._send_seq[direction] += 1
        return header + ciphertext

    def open(self, frame: bytes, expected_direction: int) -> bytes:
        if len(frame) < 10 + 16:
            raise ValueError("truncated relay frame")
        version, direction, seq = struct.unpack("!BBQ", frame[:10])
        if version != PROTOCOL_VERSION:
            raise ValueError("unsupported relay frame version")
        if direction != expected_direction:
            raise ValueError("wrong relay frame direction")
        if seq != self._recv_seq[direction]:
            raise ValueError("replayed or out-of-order relay frame")
        nonce = b"IRF" + bytes((direction,)) + struct.pack("!Q", seq)
        plain = self._aead.decrypt(
            nonce, frame[10:], self._aad_prefix + frame[:10])
        self._recv_seq[direction] += 1
        return plain
