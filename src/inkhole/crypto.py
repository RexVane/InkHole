"""
crypto.py
=========
墨洞端到端加密(AES-256-GCM)，被 p2p.py 使用。

整块格式 WHE1(小文件): magic"WHE1" + salt(16B) + nonce(12B) + AES-GCM密文(含校验标签)
分块格式 WHE2(大文件): magic"WHE2" + salt(16B) + base_nonce(12B) + 帧*
    帧 = [4B 密文长度(BE)] + 密文
    第 i 块: nonce_i = base[0:4] + BE64(BE64(base[4:12]) + i)，AAD = BE64(i)
    块序号进 nonce 和 AAD：篡改、重排、跨文件拼接都会解密失败。
    明文按 4MB 分块——收发内存峰值 4MB，特大文件不再整块进内存。

密钥由口令经 PBKDF2-HMAC-SHA256(10 万次) 派生，salt/nonce 每个文件随机。
两种格式与 Android 端 Crypto.kt 逐字节兼容。
"""

from __future__ import annotations
import os
import struct

_MAGIC = b"WHE1"
_MAGIC2 = b"WHE2"
CHUNK_SIZE = 4 * 1024 * 1024      # 分块明文大小
_CHUNK_OVERHEAD = 20              # 每帧开销: 4B 长度 + 16B GCM tag


def _derive_key(secret: str, salt: bytes) -> bytes:
    import hashlib
    # 提升迭代次数至 600,000 以抵御现代硬件暴力破解 (OWASP 推荐标准)
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 600_000, dklen=32)


# ---------- 整块格式 WHE1 ----------

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
    """检查数据是否以墨洞整块加密 magic 开头。"""
    return blob[:4] == _MAGIC


# ---------- 分块格式 WHE2 ----------

def chunked_wire_size(plain_size: int) -> int:
    """给定明文大小，返回分块加密后的线上字节总数(可预先算出，发送方写进 header)。"""
    n_chunks = (plain_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return 32 + plain_size + n_chunks * _CHUNK_OVERHEAD


def _chunk_nonce(base: bytes, idx: int) -> bytes:
    """第 idx 块的 nonce：前 4 字节固定，后 8 字节计数器 + idx(mod 2^64)。"""
    ctr = (int.from_bytes(base[4:12], "big") + idx) & 0xFFFFFFFFFFFFFFFF
    return base[:4] + ctr.to_bytes(8, "big")


def encrypt_chunks(secret: str, fileobj):
    """流式分块加密生成器：yield 线上字节(先 32B 流头，再逐帧)。

    每次只持有一个 4MB 块，内存峰值恒定。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, base = os.urandom(16), os.urandom(12)
    aes = AESGCM(_derive_key(secret, salt))
    yield _MAGIC2 + salt + base
    idx = 0
    while True:
        plain = fileobj.read(CHUNK_SIZE)
        if not plain:
            break
        ct = aes.encrypt(_chunk_nonce(base, idx), plain, struct.pack("!Q", idx))
        yield struct.pack("!I", len(ct)) + ct
        idx += 1


class ChunkedDecryptor:
    """按序解密 WHE2 分块流。

    用法：先喂 32B 流头构造；随后每读到一帧密文调 decrypt_chunk()，
    返回明文，口令不对/被篡改/被重排返回 None。
    """

    def __init__(self, secret: str, stream_header: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(stream_header) != 32 or stream_header[:4] != _MAGIC2:
            raise ValueError("bad WHE2 stream header")
        self._base = stream_header[20:32]
        self._aes = AESGCM(_derive_key(secret, stream_header[4:20]))
        self._idx = 0

    def decrypt_chunk(self, ct: bytes) -> bytes | None:
        from cryptography.exceptions import InvalidTag
        try:
            plain = self._aes.decrypt(_chunk_nonce(self._base, self._idx),
                                      ct, struct.pack("!Q", self._idx))
        except InvalidTag:
            return None
        self._idx += 1
        return plain
