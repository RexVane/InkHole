"""
crypto.py
=========
墨洞端到端加密(AES-256-GCM)，被 p2p.py 使用。

整块格式 WHE1(小文件): magic"WHE1" + salt(16B) + nonce(12B) + AES-GCM密文(含校验标签)
分块格式 WHE2/WHE3/WHE4(大文件): magic + salt(16B) + base_nonce(12B) + 帧*
    帧 = [4B 密文长度(BE)] + 密文
    第 i 块: nonce_i = base[0:4] + BE64(BE64(base[4:12]) + i)，AAD = BE64(i)
    块序号进 nonce 和 AAD：篡改、重排、跨文件拼接都会解密失败。
    明文按 4MB 分块——收发内存峰值 4MB，特大文件不再整块进内存。

WHE1/WHE2 为兼容格式，PBKDF2-HMAC-SHA256 使用 10 万次；WHE3 每流 60 万次。
WHE4(经 WHPC "whe4" 能力协商)：口令先经 60 万次 PBKDF2 对固定应用盐派生
主密钥(每进程每口令只算一次并缓存)，每流再用 HKDF-SHA256(master,
salt=流盐, info) 派生流密钥——每次传输省 ~0.5s CPU，流间隔离不变。
salt/nonce 每个文件随机，与 Go whe.go、Android Crypto.kt 逐字节兼容。
"""

from __future__ import annotations
import os
import struct
import threading

_MAGIC = b"WHE1"
_MAGIC2 = b"WHE2"
_MAGIC3 = b"WHE3"
_MAGIC4 = b"WHE4"
_LEGACY_ITERATIONS = 100_000
_ITERATIONS = 600_000
_WHE4_MASTER_SALT = b"INKHOLE-WHE4-MASTER-V1"
_WHE4_STREAM_INFO = b"INKHOLE-WHE4-STREAM-V1"
CHUNK_SIZE = 4 * 1024 * 1024      # 分块明文大小
_CHUNK_OVERHEAD = 20              # 每帧开销: 4B 长度 + 16B GCM tag

_master_lock = threading.Lock()
_master_cache: dict[bytes, bytes] = {}
# 进程随机 HMAC 键：缓存键是口令摘要而非明文口令，内存检查/诊断转储里
# 不会长期留存换掉的旧口令本身(与 Go masterCache 相同的设计)。
_master_cache_key = os.urandom(32)


def _derive_key(secret: str, salt: bytes, iterations: int = _ITERATIONS) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), salt, iterations, dklen=32)


def _master_cache_id(secret: str) -> bytes:
    import hashlib
    import hmac
    return hmac.new(_master_cache_key, secret.encode("utf-8"),
                    hashlib.sha256).digest()


def _master_key(secret: str) -> bytes:
    """WHE4 主密钥：每口令一次 60 万次 PBKDF2，进程内缓存。"""
    cache_id = _master_cache_id(secret)
    with _master_lock:
        cached = _master_cache.get(cache_id)
    if cached is not None:
        return cached
    derived = _derive_key(secret, _WHE4_MASTER_SALT)
    with _master_lock:
        _master_cache[cache_id] = derived
    return derived


def _stream_key_whe4(secret: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=_WHE4_STREAM_INFO).derive(_master_key(secret))


# ---------- 整块格式 WHE1 ----------

def encrypt(secret: str, plain: bytes) -> bytes:
    """加密明文 bytes，返回 magic+salt+nonce+密文。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive_key(secret, salt, _LEGACY_ITERATIONS)
    return _MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, plain, None)


def decrypt(secret: str, blob: bytes) -> bytes | None:
    """解密；不是加密格式或口令不对返回 None。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    if not is_encrypted(blob) or len(blob) < 4 + 16 + 12 + 16:
        return None
    salt, nonce, ct = blob[4:20], blob[20:32], blob[32:]
    try:
        key = _derive_key(secret, salt, _LEGACY_ITERATIONS)
        return AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        return None


def is_encrypted(blob: bytes) -> bool:
    """检查数据是否以墨洞整块加密 magic 开头。"""
    return blob[:4] == _MAGIC


# ---------- 分块格式 WHE2/WHE3 ----------

def chunked_wire_size(plain_size: int) -> int:
    """给定明文大小，返回分块加密后的线上字节总数(可预先算出，发送方写进 header)。"""
    n_chunks = (plain_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return 32 + plain_size + n_chunks * _CHUNK_OVERHEAD


def _chunk_nonce(base: bytes, idx: int) -> bytes:
    """第 idx 块的 nonce：前 4 字节固定，后 8 字节计数器 + idx(mod 2^64)。"""
    ctr = (int.from_bytes(base[4:12], "big") + idx) & 0xFFFFFFFFFFFFFFFF
    return base[:4] + ctr.to_bytes(8, "big")


def encrypt_chunks(secret: str, fileobj, use_whe4: bool = False):
    """流式分块加密生成器：yield 线上字节(先 32B 流头，再逐帧)。

    每次只持有一个 4MB 块，内存峰值恒定。use_whe4 仅当对端在 WHPC 中
    声明了 "whe4" 能力时为 True；否则发 WHE3，任何 v3 对端都能解。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, base = os.urandom(16), os.urandom(12)
    if use_whe4:
        aes = AESGCM(_stream_key_whe4(secret, salt))
        yield _MAGIC4 + salt + base
    else:
        aes = AESGCM(_derive_key(secret, salt))
        yield _MAGIC3 + salt + base
    idx = 0
    while True:
        plain = fileobj.read(CHUNK_SIZE)
        if not plain:
            break
        ct = aes.encrypt(_chunk_nonce(base, idx), plain, struct.pack("!Q", idx))
        yield struct.pack("!I", len(ct)) + ct
        idx += 1


class ChunkedDecryptor:
    """按序解密 WHE2/WHE3/WHE4 分块流。

    用法：先喂 32B 流头构造；随后每读到一帧密文调 decrypt_chunk()，
    返回明文，口令不对/被篡改/被重排返回 None。
    """

    def __init__(self, secret: str, stream_header: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(stream_header) != 32 or stream_header[:4] not in (
                _MAGIC2, _MAGIC3, _MAGIC4):
            raise ValueError("bad chunked encryption stream header")
        salt = stream_header[4:20]
        if stream_header[:4] == _MAGIC4:
            key = _stream_key_whe4(secret, salt)
        elif stream_header[:4] == _MAGIC2:
            key = _derive_key(secret, salt, _LEGACY_ITERATIONS)
        else:
            key = _derive_key(secret, salt, _ITERATIONS)
        self._base = stream_header[20:32]
        self._aes = AESGCM(key)
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
