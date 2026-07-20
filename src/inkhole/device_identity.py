"""Persistent device identity primitives shared by LAN discovery and WHPP."""

from __future__ import annotations

import base64
import hashlib
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


_CAP_DOMAIN = b"INKHOLE-WHPC3\0"
_TRANSFER_DOMAIN = b"INKHOLE-WHPP3-AUTH\0"
_RECEIVER_DOMAIN = b"INKHOLE-WHPP3-RECEIVER\0"


def _encoded_text(value: str, limit: int) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("identity field is too long")
    return encoded


def capability_message(nonce: bytes, instance_id: str, peer_name: str,
                       version: int, capabilities) -> bytes:
    if len(nonce) != 32:
        raise ValueError("capability nonce must be 32 bytes")
    if isinstance(version, bool) or not 0 <= int(version) <= 0xFFFF:
        raise ValueError("capability version is invalid")
    name = _encoded_text(peer_name, 0xFFFF)
    caps = sorted({_encoded_text(str(value), 0xFFFF) for value in capabilities})
    if len(caps) > 0xFFFF:
        raise ValueError("too many capabilities")
    encoded_caps = b"".join(struct.pack("!H", len(value)) + value
                            for value in caps)
    return b"".join((
        _CAP_DOMAIN, nonce, struct.pack("!H", int(version)),
        instance_id.lower().encode("ascii"), struct.pack("!H", len(name)), name,
        struct.pack("!H", len(caps)), encoded_caps,
    ))


def transfer_message(nonce: bytes, header: dict, offset: int) -> bytes:
    if len(nonce) != 32:
        raise ValueError("transfer nonce must be 32 bytes")
    filename = _encoded_text(str(header["filename"]), 0xFFFFFFFF)
    kind = _encoded_text(str(header["kind"]), 0xFFFF)
    return b"".join((
        _TRANSFER_DOMAIN,
        nonce,
        str(header["sender_instance_id"]).lower().encode("ascii"),
        str(header["transfer_id"]).lower().encode("ascii"),
        str(header["sha256"]).lower().encode("ascii"),
        struct.pack("!H", len(kind)), kind,
        struct.pack("!I", len(filename)), filename,
        struct.pack("!QQBQ", int(header["plain_size"]),
                    int(header.get("mtime_ms", 0)),
                    1 if bool(header["encrypted"]) else 0, int(offset)),
    ))


def receiver_message(nonce: bytes, header: dict, offset: int,
                     receiver_instance_id: str) -> bytes:
    """Bind a WHPP resume/ACK channel to the selected receiving device."""
    if len(nonce) != 32:
        raise ValueError("receiver nonce must be 32 bytes")
    receiver = str(receiver_instance_id).lower().encode("ascii")
    if len(receiver) != 32:
        raise ValueError("receiver instance id must be 32 bytes")
    sender_fields = transfer_message(nonce, header, offset)[
        len(_TRANSFER_DOMAIN) + len(nonce):]
    return _RECEIVER_DOMAIN + nonce + receiver + sender_fields


class DeviceIdentity:
    """ECDSA P-256 identity serialized as PKCS#8 for credential-store use."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey):
        if not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError("device identity must use P-256")
        self._private_key = private_key
        self.public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.public_key = base64.b64encode(self.public_bytes).decode("ascii")
        self.fingerprint = hashlib.sha256(self.public_bytes).hexdigest()

    @classmethod
    def generate(cls) -> "DeviceIdentity":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_key(cls, encoded: str) -> "DeviceIdentity":
        raw = base64.b64decode(str(encoded), validate=True)
        key = serialization.load_der_private_key(raw, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("device identity is not an EC private key")
        return cls(key)

    def export_private_key(self) -> str:
        raw = self._private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return base64.b64encode(raw).decode("ascii")

    def sign(self, message: bytes) -> str:
        signature = self._private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature).decode("ascii")


def public_fingerprint(encoded: str) -> str:
    raw = base64.b64decode(str(encoded), validate=True)
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("device public key is not an uncompressed P-256 point")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    return hashlib.sha256(raw).hexdigest()


def verify(encoded_public_key: str, message: bytes, encoded_signature: str) -> bool:
    try:
        raw_key = base64.b64decode(str(encoded_public_key), validate=True)
        signature = base64.b64decode(str(encoded_signature), validate=True)
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), raw_key)
        public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False
