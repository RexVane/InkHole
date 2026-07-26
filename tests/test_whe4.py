"""WHE4 跨端一致性与回归测试。

KAT 向量与 transport-core/core/lan/whe_test.go、Android CryptoTest.kt
共用同一 stream_hex——三端解出同一明文，防实现漂移。
"""
import io

from inkhole.crypto import (
    ChunkedDecryptor,
    encrypt_chunks,
    _master_cache,
    _master_cache_id,
)

KAT_SECRET = "kat-秘密-2026"
KAT_PLAIN = "墨洞 WHE4 known-answer test payload"
KAT_STREAM_HEX = (
    "57484534303132333435363738396162636465664b41546e6f6e63652f313242"
    "000000359ffa94d1a917a59c125e3cb007bbc7c4fea5ec27c482e87d9417ef98"
    "f5363211904eea1ba1f6147c5daf8a44400d341e6e7eec3e24"
)


def _split_stream(stream: bytes):
    header, rest = stream[:32], stream[32:]
    frames = []
    while rest:
        size = int.from_bytes(rest[:4], "big")
        frames.append(rest[4:4 + size])
        rest = rest[4 + size:]
    return header, frames


def test_whe4_known_answer():
    header, frames = _split_stream(bytes.fromhex(KAT_STREAM_HEX))
    decryptor = ChunkedDecryptor(KAT_SECRET, header)
    plain = decryptor.decrypt_chunk(frames[0])
    assert plain is not None
    assert plain.decode("utf-8") == KAT_PLAIN


def test_whe4_round_trip_and_master_cache():
    payload = "whe4-负载".encode("utf-8") * 300
    stream = b"".join(encrypt_chunks("回环口令", io.BytesIO(payload), use_whe4=True))
    header, frames = _split_stream(stream)
    assert header[:4] == b"WHE4"
    # 主密钥已缓存(后续传输免 60 万次派生)，且缓存键是进程随机 HMAC
    # 摘要——明文口令绝不能出现在缓存键里
    assert _master_cache_id("回环口令") in _master_cache
    assert all(not isinstance(key, str) for key in _master_cache)
    decryptor = ChunkedDecryptor("回环口令", header)
    assert decryptor.decrypt_chunk(frames[0]) == payload
    # 重放同一帧必须失败
    assert decryptor.decrypt_chunk(frames[0]) is None
    # 错口令必须失败
    assert ChunkedDecryptor("错口令", header).decrypt_chunk(frames[0]) is None


def test_whe4_defaults_to_whe3_without_negotiation():
    stream = b"".join(encrypt_chunks("口令", io.BytesIO(b"x")))
    assert stream[:4] == b"WHE3"
