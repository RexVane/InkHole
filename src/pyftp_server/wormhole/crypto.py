"""
crypto.py
=========
虫洞端到端加密(AES-256-GCM)，被 sync.py(FTP 中转) 和 p2p.py(局域网直连) 共用。

加密文件格式: magic"WHE1" + salt(16B) + nonce(12B) + AES-GCM密文(含校验标签)
密钥由口令经 PBKDF2-HMAC-SHA256 派生，salt/nonce 每个文件随机。
传输全程只见密文——即使中转服务器/网络窃听者都看不到内容。
"""

from __future__ import annotations
import os

_MAGIC = b"WHE1"


def _derive_key(secret: str, salt: bytes) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000, dklen=32)


def encrypt(secret: str, plain: bytes) -> bytes:
    """加密明文 bytes，返回 magic+salt+nonce+密文。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    return _MAGIC + salt + nonce + AESGCM(_derive_key(secret, salt)).encrypt(nonce, plain, None)


def decrypt(secret: str, blob: bytes) -> bytes | None:
    """解密；不是加密格式或口令不对返回 None。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    if not is_encrypted(blob) or len(blob) < 4 + 16 + 12 + 16:
        return None
    salt, nonce, ct = blob[4:20], blob[20:32], blob[32:]
    try:
        return AESGCM(_derive_key(secret, salt)).decrypt(nonce, ct, None)
    except InvalidTag:
        return None


def is_encrypted(blob: bytes) -> bool:
    """检查数据是否以虫洞加密 magic 开头。"""
    return blob[:4] == _MAGIC
