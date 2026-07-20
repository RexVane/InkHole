#!/usr/bin/env python3
"""
test_p2p.py
===========
墨洞 P2P 引擎端到端测试。

测试覆盖：
  1. TCP 直连传输（绕过 mDNS，手动注册对端）
  2. 端到端加密传输
  3. 手动切换目标设备
  4. 对端离线（选中设备不自动切换，由用户重新选）
  5. 回调触发
  6. 多文件连续发送
  7. 路径穿越防御
  8. 半截文件不落盘（对端中途断连）
  9. 恶意 size 拒收
 10. 同显示名设备共存 + 按服务名精确离线
 11. 口令不一致时发送方感知失败（ACK）
 12. 传输进度回调

运行：PYTHONPATH=src python3 tests/test_p2p.py
      （也兼容 pytest：check 失败会以 AssertionError 上报）
"""

import os
import sys
import time
import shutil
import struct
import json
import socket
import tempfile
import threading
import uuid
import hashlib
import io

# 把 src 加入 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inkhole.p2p as p2p_module
from inkhole.p2p import (P2PNode, P2PConfig, PeerInfo, _MAGIC,
                        _probe_peer, inbox_category_for, inbox_root_for)
from inkhole.device_identity import (DeviceIdentity, receiver_message,
                                     transfer_message, verify)
from inkhole.crypto import CHUNK_SIZE, chunked_wire_size, encrypt_chunks


_TEST_IDENTITY = DeviceIdentity.generate()
_TEST_INSTANCE_ID = uuid.uuid4().hex


# ---------- 测试框架 ----------
_passed = 0
_failed = 0

def check(name, cond):
    """断言式检查：失败打印并抛 AssertionError(当前测试中止，主入口继续跑下一组)。"""
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")
        raise AssertionError(name)


# ---------- 工具 ----------
def make_node(tmpdir, name="test", secret="", port=0,
              encryption_enabled=None):
    """创建一个 P2P 节点，收件箱在 tmpdir 下。

    enable_mdns=False：只起 TCP 层，手动注册对端。测试不碰真实 mDNS，
    否则测试节点会互相发现、局域网里真实运行的墨洞也会污染结果。
    """
    inbox = os.path.join(tmpdir, name + "_inbox")
    cfg = P2PConfig(inbox=inbox, listen_port=port, peer_name=name,
                    secret=secret, enable_mdns=False,
                    encryption_enabled=encryption_enabled)
    return P2PNode(cfg)


def send_v3_payload(sock, filename, payload, kind="file", plain_size=None,
                    expected_digest=None, identity=_TEST_IDENTITY,
                    instance_id=_TEST_INSTANCE_ID):
    """Drive the WHPP v3 control handshake for receiver-defense tests."""
    total = len(payload) if plain_size is None else plain_size
    digest = expected_digest or hashlib.sha256(payload).hexdigest()
    transfer_id = hashlib.sha256(
        f"test:{kind}:{filename}:{digest}".encode()).hexdigest()
    header = {
        "version": 3,
        "filename": filename,
        "plain_size": total,
        "transfer_id": transfer_id,
        "sha256": digest,
        "kind": kind,
        "encrypted": False,
        "want_ack": True,
        "sender_instance_id": instance_id,
        "sender_public_key": identity.public_key,
    }
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    sock.sendall(_MAGIC + struct.pack("!I", len(encoded)) + encoded)
    marker, offset, nonce = recv_resume(sock, header)
    if marker != b"\x02":
        return marker
    signature = identity.sign(transfer_message(nonce, header, offset)).encode("ascii")
    sock.sendall(struct.pack("!H", len(signature)) + signature)
    remaining = payload[offset:]
    sock.sendall(struct.pack("!Q", total - offset) + remaining)
    return sock.recv(1)


def recv_exact(sock, size):
    value = bytearray()
    while len(value) < size:
        chunk = sock.recv(size - len(value))
        if not chunk:
            raise EOFError("socket closed")
        value.extend(chunk)
    return bytes(value)


def recv_resume(sock, header):
    marker = recv_exact(sock, 1)
    if marker != b"\x02":
        return marker, 0, b""
    offset = struct.unpack("!Q", recv_exact(sock, 8))[0]
    nonce = recv_exact(sock, 32)
    receiver_instance_id = recv_exact(sock, 32).decode("ascii")
    public_size = struct.unpack("!H", recv_exact(sock, 2))[0]
    public_key = recv_exact(sock, public_size).decode("ascii")
    signature_size = struct.unpack("!H", recv_exact(sock, 2))[0]
    signature = recv_exact(sock, signature_size).decode("ascii")
    assert verify(public_key, receiver_message(
        nonce, header, offset, receiver_instance_id), signature)
    return marker, offset, nonce


def raw_v3_header(filename, payload, identity=_TEST_IDENTITY,
                  instance_id=_TEST_INSTANCE_ID, encrypted=False,
                  transfer_id=None):
    return {
        "version": 3,
        "filename": filename,
        "plain_size": len(payload),
        "transfer_id": transfer_id or hashlib.sha256(os.urandom(32)).hexdigest(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": "file",
        "encrypted": encrypted,
        "enc_mode": "chunked" if encrypted else "",
        "want_ack": True,
        "sender_instance_id": instance_id,
        "sender_public_key": identity.public_key,
    }


def begin_raw_v3(sock, header, identity=_TEST_IDENTITY):
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    sock.sendall(_MAGIC + struct.pack("!I", len(encoded)) + encoded)
    marker, offset, nonce = recv_resume(sock, header)
    assert marker == b"\x02"
    signature = identity.sign(
        transfer_message(nonce, header, offset)).encode("ascii")
    sock.sendall(struct.pack("!H", len(signature)) + signature)
    return offset


def test_probe_rejects_non_object_json_response():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _addr = listener.accept()
        with conn:
            assert recv_exact(conn, 36)[:4] == b"WHPC"
            body = b"[]"
            conn.sendall(b"WHPC" + struct.pack("!I", len(body)) + body)
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        try:
            _probe_peer("127.0.0.1", listener.getsockname()[1], 2)
        except OSError as exc:
            assert "格式非法" in str(exc)
        else:
            raise AssertionError("non-object WHPC response was accepted")
    finally:
        thread.join(2)


def test_plain_transfer_resumes_from_persisted_offset(tmp_path):
    payload = os.urandom(512 * 1024)
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Receiver", enable_mdns=False))
    node.start()
    try:
        header = raw_v3_header("resume.bin", payload)
        first = socket.create_connection(("127.0.0.1", node.actual_port), timeout=3)
        assert begin_raw_v3(first, header) == 0
        half = len(payload) // 2
        first.sendall(struct.pack("!Q", len(payload)) + payload[:half])
        first.close()
        part = tmp_path / f".inkhole-{header['transfer_id']}.part"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
                not part.exists() or part.stat().st_size != half):
            time.sleep(0.01)
        assert part.stat().st_size == half

        with socket.create_connection(("127.0.0.1", node.actual_port), timeout=3) as second:
            assert begin_raw_v3(second, header) == half
            second.sendall(struct.pack("!Q", len(payload) - half) + payload[half:])
            assert recv_exact(second, 33) == b"\x01" + bytes.fromhex(header["sha256"])
        assert (tmp_path / "resume.bin").read_bytes() == payload
    finally:
        node.stop()


def test_encrypted_transfer_resumes_with_fresh_suffix_stream(tmp_path):
    payload = os.urandom(CHUNK_SIZE + 1024)
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Receiver", enable_mdns=False,
        secret="resume-secret"))
    node.start()
    try:
        header = raw_v3_header("encrypted.bin", payload, encrypted=True)
        first_wire = list(encrypt_chunks("resume-secret", io.BytesIO(payload)))
        with socket.create_connection(("127.0.0.1", node.actual_port), timeout=5) as first:
            assert begin_raw_v3(first, header) == 0
            first.sendall(struct.pack("!Q", chunked_wire_size(len(payload))))
            first.sendall(first_wire[0] + first_wire[1])
        part = tmp_path / f".inkhole-{header['transfer_id']}.part"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
                not part.exists() or part.stat().st_size != CHUNK_SIZE):
            time.sleep(0.01)
        assert part.stat().st_size == CHUNK_SIZE

        suffix = payload[CHUNK_SIZE:]
        suffix_wire = b"".join(encrypt_chunks("resume-secret", io.BytesIO(suffix)))
        with socket.create_connection(("127.0.0.1", node.actual_port), timeout=5) as second:
            assert begin_raw_v3(second, header) == CHUNK_SIZE
            second.sendall(struct.pack("!Q", len(suffix_wire)) + suffix_wire)
            assert recv_exact(second, 33) == b"\x01" + bytes.fromhex(header["sha256"])
        assert (tmp_path / "encrypted.bin").read_bytes() == payload
    finally:
        node.stop()


def test_lost_ack_retry_reuses_receipt_without_duplicate(tmp_path, monkeypatch):
    payload = b"receipt-retry"
    receipt_started = threading.Event()
    release_receipt = threading.Event()
    original_write_json = p2p_module._write_json_atomic

    def delay_completion_receipt(path, value):
        if path.endswith(".done.json"):
            receipt_started.set()
            assert release_receipt.wait(3)
        original_write_json(path, value)

    monkeypatch.setattr(p2p_module, "_write_json_atomic", delay_completion_receipt)
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Receiver", enable_mdns=False))
    node.start()
    try:
        header = raw_v3_header("lost-ack.txt", payload)
        first = socket.create_connection(("127.0.0.1", node.actual_port), timeout=3)
        assert begin_raw_v3(first, header) == 0
        first.sendall(struct.pack("!Q", len(payload)) + payload)
        first.close()
        destination = tmp_path / "lost-ack.txt"
        assert receipt_started.wait(3)
        assert destination.read_bytes() == payload

        retry_result = []

        def retry_transfer():
            with socket.create_connection(
                    ("127.0.0.1", node.actual_port), timeout=3) as retry:
                retry_result.append(begin_raw_v3(retry, header))
                retry.sendall(struct.pack("!Q", 0))
                retry_result.append(recv_exact(retry, 33))

        retry_thread = threading.Thread(target=retry_transfer)
        retry_thread.start()
        time.sleep(0.05)
        assert retry_thread.is_alive()
        release_receipt.set()
        retry_thread.join(3)
        assert not retry_thread.is_alive()
        assert retry_result == [
            len(payload), b"\x01" + bytes.fromhex(header["sha256"])]
        assert sorted(path.name for path in tmp_path.glob("lost-ack*")) == ["lost-ack.txt"]
    finally:
        release_receipt.set()
        node.stop()


def test_lost_ack_receipt_survives_destination_move(tmp_path):
    payload = b"exported-before-ack-retry"
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path), peer_name="Receiver", enable_mdns=False))
    node.start()
    try:
        header = raw_v3_header("exported.txt", payload)
        first = socket.create_connection(("127.0.0.1", node.actual_port), timeout=3)
        assert begin_raw_v3(first, header) == 0
        first.sendall(struct.pack("!Q", len(payload)) + payload)
        first.close()
        destination = tmp_path / "exported.txt"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not destination.exists():
            time.sleep(0.01)
        assert destination.read_bytes() == payload

        exported = tmp_path / "public-downloads.txt"
        destination.replace(exported)
        with socket.create_connection(("127.0.0.1", node.actual_port), timeout=3) as retry:
            assert begin_raw_v3(retry, header) == len(payload)
            retry.sendall(struct.pack("!Q", 0))
            assert recv_exact(retry, 33) == b"\x01" + bytes.fromhex(header["sha256"])

        assert exported.read_bytes() == payload
        assert not destination.exists()
    finally:
        node.stop()


def test_sender_does_not_succeed_when_ack_connection_resets(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(3)

    receiver_identity = DeviceIdentity.generate()
    receiver_instance_id = uuid.uuid4().hex

    def reject_acks():
        for _ in range(3):
            conn, _ = listener.accept()
            with conn:
                assert recv_exact(conn, 4) == _MAGIC
                header_size = struct.unpack("!I", recv_exact(conn, 4))[0]
                header = json.loads(recv_exact(conn, header_size))
                nonce = os.urandom(32)
                public_key = receiver_identity.public_key.encode("ascii")
                receiver_signature = receiver_identity.sign(receiver_message(
                    nonce, header, 0, receiver_instance_id)).encode("ascii")
                conn.sendall(b"".join((
                    b"\x02", struct.pack("!Q", 0), nonce,
                    receiver_instance_id.encode("ascii"),
                    struct.pack("!H", len(public_key)), public_key,
                    struct.pack("!H", len(receiver_signature)), receiver_signature,
                )))
                signature_size = struct.unpack("!H", recv_exact(conn, 2))[0]
                recv_exact(conn, signature_size)
                body_size = struct.unpack("!Q", recv_exact(conn, 8))[0]
                recv_exact(conn, body_size)
                assert header["plain_size"] == body_size
        listener.close()

    thread = threading.Thread(target=reject_acks, daemon=True)
    thread.start()
    sent = []
    node = P2PNode(P2PConfig(inbox=str(tmp_path / "inbox"), enable_mdns=False),
                   on_sent=sent.append)
    node._on_peer_added(
        "Reset", "127.0.0.1", listener.getsockname()[1],
        instance_id=receiver_instance_id, capabilities={"reliable-v3"},
        public_key=receiver_identity.public_key,
        identity_fingerprint=receiver_identity.fingerprint)
    node.select_peer("Reset")
    source = tmp_path / "source.bin"
    source.write_bytes(b"never-success")
    assert node.send_file(str(source)) is False
    thread.join(3)
    assert sent == []


def test_sender_rejects_receiver_identity_mismatch_before_body(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(3)
    expected_identity = DeviceIdentity.generate()
    attacker_identity = DeviceIdentity.generate()
    receiver_instance_id = uuid.uuid4().hex
    body_seen = []

    def impersonate_receiver():
        for _ in range(3):
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(1)
                assert recv_exact(conn, 4) == _MAGIC
                header_size = struct.unpack("!I", recv_exact(conn, 4))[0]
                header = json.loads(recv_exact(conn, header_size))
                nonce = os.urandom(32)
                public_key = attacker_identity.public_key.encode("ascii")
                signature = attacker_identity.sign(receiver_message(
                    nonce, header, 0, receiver_instance_id)).encode("ascii")
                conn.sendall(b"".join((
                    b"\x02", struct.pack("!Q", 0), nonce,
                    receiver_instance_id.encode("ascii"),
                    struct.pack("!H", len(public_key)), public_key,
                    struct.pack("!H", len(signature)), signature,
                )))
                try:
                    body_seen.append(bool(conn.recv(1)))
                except (ConnectionResetError, socket.timeout):
                    body_seen.append(False)
        listener.close()

    thread = threading.Thread(target=impersonate_receiver, daemon=True)
    thread.start()
    node = P2PNode(P2PConfig(
        inbox=str(tmp_path / "inbox"), enable_mdns=False))
    node._on_peer_added(
        "Pinned", "127.0.0.1", listener.getsockname()[1],
        instance_id=receiver_instance_id, capabilities={"reliable-v3"},
        public_key=expected_identity.public_key,
        identity_fingerprint=expected_identity.fingerprint)
    node.select_peer("Pinned")
    source = tmp_path / "secret.bin"
    source.write_bytes(b"must not be sent")

    assert node.send_file(str(source)) is False
    thread.join(5)
    assert body_seen == [False, False, False]


def test_intentional_repeat_send_creates_unique_destination(tmp_path):
    sender = P2PNode(P2PConfig(
        inbox=str(tmp_path / "sender"), peer_name="Sender", enable_mdns=False))
    receiver = P2PNode(P2PConfig(
        inbox=str(tmp_path / "receiver"), peer_name="Receiver", enable_mdns=False))
    sender.start()
    receiver.start()
    try:
        sender._on_peer_added("Receiver", "127.0.0.1", receiver.actual_port)
        sender.select_peer("Receiver")
        source = tmp_path / "repeat.txt"
        source.write_text("same contents", encoding="utf-8")
        assert sender.send_file(str(source))
        assert sender.send_file(str(source))
        names = sorted(path.name for path in (tmp_path / "receiver").glob("repeat*"))
        assert names == ["repeat (2).txt", "repeat.txt"]
    finally:
        sender.stop()
        receiver.stop()


def test_stale_transfer_artifacts_are_cleaned_as_a_group(tmp_path):
    stale_id = "1" * 64
    fresh_id = "2" * 64
    stale_paths = [
        tmp_path / f".inkhole-{stale_id}.part",
        tmp_path / f".inkhole-{stale_id}.json",
        tmp_path / f".inkhole-{stale_id}.done.json",
    ]
    fresh_paths = [
        tmp_path / f".inkhole-{fresh_id}.part",
        tmp_path / f".inkhole-{fresh_id}.json",
    ]
    for path in stale_paths + fresh_paths:
        path.write_text("{}", encoding="utf-8")
    old = time.time() - 8 * 24 * 60 * 60
    for path in stale_paths + [fresh_paths[1]]:
        os.utime(path, (old, old))
    staging = tmp_path / ".inkhole-abandoned.folder.part"
    staging.mkdir()
    os.utime(staging, (old, old))
    outgoing = tmp_path / ".inkhole-outgoing.json"
    outgoing.write_text(json.dumps({
        "old": {"transfer_id": stale_id, "updated_at": int(old)},
        "fresh": {"transfer_id": fresh_id, "updated_at": int(time.time())},
    }), encoding="utf-8")

    P2PNode(P2PConfig(inbox=str(tmp_path), enable_mdns=False))

    assert not any(path.exists() for path in stale_paths)
    assert all(path.exists() for path in fresh_paths)
    assert not staging.exists()
    assert list(json.loads(outgoing.read_text(encoding="utf-8"))) == ["fresh"]


def test_encryption_can_be_disabled_without_discarding_secret(tmp_path):
    cfg = P2PConfig(inbox=str(tmp_path), secret="saved-secret",
                    encryption_enabled=False)

    assert cfg.secret == "saved-secret"
    assert cfg.active_secret == ""


def test_inbox_category_resolution(tmp_path):
    cfg = P2PConfig(
        inbox=str(tmp_path / "inbox"),
        inbox_auto_classify=True,
        inbox_category_dirs={"media": str(tmp_path / "custom-media")},
    )

    assert inbox_category_for("photo.HEIC") == "media"
    assert inbox_category_for("clip.mp4") == "media"
    assert inbox_category_for("backup.tar.gz") == "archive"
    assert inbox_category_for("notes.pdf") == "file"
    assert inbox_category_for("archive.zip", "folder-v1") == "folder"
    assert inbox_root_for(cfg, "photo.jpg") == str(tmp_path / "custom-media")
    assert inbox_root_for(cfg, "backup.zip") == str(tmp_path / "inbox" / "压缩包")


def test_automatic_inbox_classification_receives_into_target_directory(tmp_path):
    sender = make_node(str(tmp_path), "Alice")
    receiver = P2PNode(P2PConfig(
        inbox=str(tmp_path / "Bob_inbox"),
        peer_name="Bob",
        enable_mdns=False,
        inbox_auto_classify=True,
        inbox_category_dirs={"media": str(tmp_path / "photos")},
    ))
    try:
        sender.start()
        receiver.start()
        sender._on_peer_added("Bob", "127.0.0.1", receiver.actual_port)
        sender.select_peer("Bob")

        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"image-content")
        assert sender.send_file(str(photo))
        assert wait_for_file(str(tmp_path / "photos"), photo.name) is not None

        document = tmp_path / "notes.pdf"
        document.write_bytes(b"document-content")
        assert sender.send_file(str(document))
        default_file_root = tmp_path / "Bob_inbox" / "文件"
        assert wait_for_file(str(default_file_root), document.name) is not None
    finally:
        sender.stop()
        receiver.stop()


def test_disabled_encryption_sends_plaintext_with_saved_secret(tmp_path):
    node_a = make_node(str(tmp_path), "Alice", secret="saved-secret",
                       encryption_enabled=False)
    node_b = make_node(str(tmp_path), "Bob")
    try:
        node_a.start()
        node_b.start()
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        source = tmp_path / "plain.txt"
        source.write_text("encryption disabled", encoding="utf-8")

        assert node_a.send_file(str(source))
        received = wait_for_file(node_b.cfg.inbox, source.name)
        assert received is not None
        with open(received, encoding="utf-8") as received_file:
            assert received_file.read() == "encryption disabled"
    finally:
        node_a.stop()
        node_b.stop()


def wait_for_peer(node, timeout=5.0):
    """等待节点发现至少一个对端。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if node.peers():
            return True
        time.sleep(0.2)
    return False


def wait_for_file(inbox, filename, timeout=5.0):
    """等待收件箱里出现指定文件。"""
    deadline = time.time() + timeout
    path = os.path.join(inbox, filename)
    while time.time() < deadline:
        if os.path.isfile(path):
            return path
        time.sleep(0.2)
    return None


def wait_for_directory(inbox, name, timeout=5.0):
    deadline = time.time() + timeout
    path = os.path.join(inbox, name)
    while time.time() < deadline:
        if os.path.isdir(path):
            return path
        time.sleep(0.2)
    return None


# ---------- 测试 1: TCP 直连传输 ----------
def test_direct_transfer():
    print("\n=== 测试 1: TCP 直连传输 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)  # 等 TCP 监听就绪

        # 手动注册对端（绕过 mDNS）
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_b._on_peer_added("Alice", "127.0.0.1", node_a.actual_port)

        check("Alice 发现了 Bob", len(node_a.peers()) == 1)
        check("Bob 发现了 Alice", len(node_b.peers()) == 1)

        # 不自动选中，需手动选择
        check("未自动选中(需手动选)", node_a.selected_peer() is None)
        node_a.select_peer("Bob")

        # 发送文件
        src = os.path.join(tmpdir, "hello.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Hello from Alice!\n你好，墨洞！")

        ok = node_a.send_file(src)
        check("send_file 返回 True", ok)

        # 等待 Bob 收到
        recv = wait_for_file(node_b.cfg.inbox, "hello.txt")
        check("Bob 收到文件", recv is not None)
        if recv:
            with open(recv, "r", encoding="utf-8") as f:
                content = f.read()
            check("文件内容一致", content == "Hello from Alice!\n你好，墨洞！")

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 2: 端到端加密 ----------
def test_encrypted_transfer():
    print("\n=== 测试 2: 端到端加密传输 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        secret = "my-secret-passphrase"
        node_a = make_node(tmpdir, "Alice", secret=secret)
        node_b = make_node(tmpdir, "Bob", secret=secret)
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        # 发送文件
        src = os.path.join(tmpdir, "secret.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("这是一段机密内容 🔒")

        ok = node_a.send_file(src)
        check("加密发送成功", ok)

        recv = wait_for_file(node_b.cfg.inbox, "secret.txt")
        check("Bob 收到加密文件", recv is not None)
        if recv:
            with open(recv, "r", encoding="utf-8") as f:
                content = f.read()
            check("解密后内容一致", content == "这是一段机密内容 🔒")

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 3: 切换目标设备 ----------
def test_peer_selection():
    print("\n=== 测试 3: 切换目标设备 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_c = make_node(tmpdir, "Carol")
        node_a.start()
        node_b.start()
        node_c.start()
        time.sleep(0.3)

        # Alice 同时发现 Bob 和 Carol
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a._on_peer_added("Carol", "127.0.0.1", node_c.actual_port)

        check("Alice 发现 2 台设备", len(node_a.peers()) == 2)
        check("未自动选中", node_a.selected_peer() is None)

        # 手动切换到 Carol
        node_a.select_peer("Carol")
        check("手动选中 Carol", node_a.selected_peer() == "Carol")

        # 发给 Carol
        src = os.path.join(tmpdir, "to_carol.txt")
        with open(src, "w") as f:
            f.write("Hi Carol!")

        node_a.send_file(src)
        recv_c = wait_for_file(node_c.cfg.inbox, "to_carol.txt")
        check("Carol 收到文件", recv_c is not None)

        # Bob 不应该收到
        recv_b = wait_for_file(node_b.cfg.inbox, "to_carol.txt", timeout=1.0)
        check("Bob 没收到(只发给 Carol)", recv_b is None)

        # 取消选择
        node_a.select_peer(None)
        check("取消选择后为 None", node_a.selected_peer() is None)

        node_a.stop()
        node_b.stop()
        node_c.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 4: 对端离线 ----------
def test_peer_offline():
    print("\n=== 测试 4: 对端离线 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_c = make_node(tmpdir, "Carol")
        node_a.start()
        node_b.start()
        node_c.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a._on_peer_added("Carol", "127.0.0.1", node_c.actual_port)

        # 发现设备不自动选中，需手动选择
        check("发现 2 台设备", len(node_a.peers()) == 2)
        check("未自动选中", node_a.selected_peer() is None)

        # 手动选中 Bob
        node_a.select_peer("Bob")
        check("手动选中 Bob", node_a.selected_peer() == "Bob")

        # Bob 离线：选中被清空，不自动切换到 Carol
        node_a._on_peer_removed("Bob")
        check("Bob 离线后选中清空(不自动切)", node_a.selected_peer() is None)
        check("对端列表只剩 Carol", len(node_a.peers()) == 1)

        # Carol 也离线
        node_a._on_peer_removed("Carol")
        check("全部离线后 selected=None", node_a.selected_peer() is None)
        check("对端列表为空", len(node_a.peers()) == 0)

        node_a.stop()
        node_b.stop()
        node_c.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 5: 回调触发 ----------
def test_callbacks():
    print("\n=== 测试 5: 回调触发 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        received_files = []
        sent_files = []
        status_msgs = []

        node_a = make_node(tmpdir, "Alice")
        node_b = P2PNode(
            P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"), peer_name="Bob",
                      enable_mdns=False),
            on_received=lambda p: received_files.append(p),
            on_status=lambda s: status_msgs.append(s),
        )
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        # 设置 A 的回调
        node_a.on_sent = lambda n: sent_files.append(n)

        src = os.path.join(tmpdir, "callback_test.txt")
        with open(src, "w") as f:
            f.write("test")

        node_a.send_file(src)
        time.sleep(1.0)

        check("on_sent 回调触发", len(sent_files) == 1)
        check("on_received 回调触发", len(received_files) == 1)
        check("on_status 回调触发", len(status_msgs) > 0)

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 6: 多文件连续发送 ----------
def test_multiple_files():
    print("\n=== 测试 6: 多文件连续发送 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        # 发 5 个文件
        filenames = [f"file_{i}.txt" for i in range(5)]
        for fn in filenames:
            src = os.path.join(tmpdir, fn)
            with open(src, "w") as f:
                f.write(f"content {fn}")

        for fn in filenames:
            node_a.send_file(os.path.join(tmpdir, fn))

        time.sleep(2.0)

        received = 0
        for fn in filenames:
            if os.path.isfile(os.path.join(node_b.cfg.inbox, fn)):
                received += 1
        check(f"5 个文件全部收到 ({received}/5)", received == 5)

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 7: 路径穿越防御 ----------
def test_path_traversal():
    print("\n=== 测试 7: 路径穿越防御 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)

        # 手动构造恶意请求：filename 含 ../
        sock = socket.create_connection(("127.0.0.1", node_b.actual_port), timeout=5)
        check("新版回执确认落盘", send_v3_payload(
            sock, "../../../evil.txt", b"evil") == b"\x01")
        sock.close()
        time.sleep(0.5)

        # 文件名应被 basename 裁剪为 evil.txt，落在 inbox 内
        evil_path = os.path.join(node_b.cfg.inbox, "evil.txt")
        check("路径穿越被防御(basename 裁剪)", os.path.isfile(evil_path))

        # 确认没有逃出 inbox
        traversal = os.path.join(tmpdir, "..", "..", "..", "evil.txt")
        check("未逃出收件箱", not os.path.isfile(os.path.abspath(traversal)))

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 8: 半截文件不落盘 ----------
def test_partial_transfer_not_delivered():
    print("\n=== 测试 8: 半截文件不落盘 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        received = []
        transfer_ends = []
        transfer_done = threading.Event()
        node_b = P2PNode(
            P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"), peer_name="Bob",
                      enable_mdns=False),
            on_received=lambda p: received.append(p),
            on_transfer_end=lambda kind, name, completed: (
                transfer_ends.append((kind, name, completed)), transfer_done.set()),
        )
        node_b.start()
        time.sleep(0.3)

        # 收件箱里已有同名完整文件——半截文件绝不能把它覆盖掉
        existing = os.path.join(node_b.cfg.inbox, "report.txt")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("完整的旧文件")

        # 声明 100 字节但只发 50 字节就断开(模拟发送方中途崩溃/断网)
        sock = socket.create_connection(("127.0.0.1", node_b.actual_port), timeout=5)
        expected = hashlib.sha256(b"x" * 100).hexdigest()
        header = {
            "version": 3, "filename": "report.txt", "plain_size": 100,
            "transfer_id": hashlib.sha256(b"partial-test").hexdigest(),
            "sha256": expected, "kind": "file", "encrypted": False,
            "want_ack": True,
            "sender_instance_id": _TEST_INSTANCE_ID,
            "sender_public_key": _TEST_IDENTITY.public_key,
        }
        encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
        sock.sendall(_MAGIC + struct.pack("!I", len(encoded)) + encoded)
        marker, offset, nonce = recv_resume(sock, header)
        check("接收方返回从零续传", marker == b"\x02" and offset == 0)
        signature = _TEST_IDENTITY.sign(
            transfer_message(nonce, header, 0)).encode("ascii")
        sock.sendall(struct.pack("!H", len(signature)) + signature)
        sock.sendall(struct.pack("!Q", 100) + b"x" * 50)
        sock.close()
        check("接收中断结束回调触发", transfer_done.wait(timeout=5))

        with open(existing, "r", encoding="utf-8") as f:
            content = f.read()
        check("旧文件未被半截文件覆盖", content == "完整的旧文件")
        check("on_received 未触发", len(received) == 0)
        check("接收结束回调标记失败",
              transfer_ends == [("recv", "report.txt", False)])
        checkpoints = [name for name in os.listdir(node_b.cfg.inbox)
                       if name.endswith(".part")]
        check("半截数据保留为续传检查点", len(checkpoints) == 1)
        check("续传检查点大小正确",
              os.path.getsize(os.path.join(node_b.cfg.inbox, checkpoints[0])) == 50)

        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 9: 恶意 size 拒收 ----------
def test_malicious_size_rejected():
    print("\n=== 测试 9: 恶意 size 拒收 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_b = make_node(tmpdir, "Bob")
        node_b.start()
        time.sleep(0.3)

        def send_raw(filename, size):
            sock = socket.create_connection(("127.0.0.1", node_b.actual_port), timeout=5)
            header = json.dumps({"filename": filename, "size": size,
                                 "encrypted": False}).encode("utf-8")
            sock.sendall(_MAGIC)
            sock.sendall(struct.pack("!I", len(header)))
            sock.sendall(header)
            sock.close()

        send_raw("neg.txt", -1)               # 负数
        send_raw("huge.txt", 1 << 50)         # 超过 1TB 上限
        send_raw("str.txt", "999")            # 字符串
        send_raw("bool.txt", True)            # bool(json 里是 true)
        time.sleep(0.8)

        inbox_files = os.listdir(node_b.cfg.inbox)
        check(f"非法 size 一个都没落盘 ({inbox_files})", inbox_files == [])

        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 10: 同显示名设备共存 + 按服务名离线 ----------
def test_duplicate_names_and_service_removal():
    print("\n=== 测试 10: 同显示名设备 + 按服务名离线 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_a.start()
        time.sleep(0.2)

        # 两台不同设备(不同服务名)取了同一个显示名
        node_a._on_peer_added("Pixel", "10.0.0.2", 1001, service_name="Pixel-aaaa._inkhole._tcp.local.")
        node_a._on_peer_added("Pixel", "10.0.0.3", 1002, service_name="Pixel-bbbb._inkhole._tcp.local.")
        names = sorted(node_a.peer_names())
        check(f"两台同名设备都在列表({names})", names == ["Pixel", "Pixel (2)"])

        # 同一服务重复通告(IP 变了)：原地更新，不新增条目
        node_a._on_peer_added("Pixel", "10.0.0.9", 1003, service_name="Pixel-aaaa._inkhole._tcp.local.")
        check("重复通告不新增条目", len(node_a.peers()) == 2)
        pixel = next(p for p in node_a.peers() if p.name == "Pixel")
        check("地址已更新", pixel.host == "10.0.0.9" and pixel.port == 1003)

        # 第二台离线：按服务名精确删除，第一台不受影响
        node_a.select_peer("Pixel (2)")
        node_a._on_peer_removed_by_service("Pixel-bbbb._inkhole._tcp.local.")
        check("离线的是 Pixel (2)", node_a.peer_names() == ["Pixel"])
        check("选中的离线后清空", node_a.selected_peer() is None)

        # 老版本对端(无服务名记录)：回退按服务名前缀解析
        node_a._on_peer_added("OldPC", "10.0.0.4", 1004)   # 手动注册，无 service_name
        node_a._on_peer_removed_by_service("OldPC._inkhole._tcp.local.")
        check("老版本对端也能正确离线", "OldPC" not in node_a.peer_names())

        node_a.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 11: 口令不一致，发送方感知失败(ACK) ----------
def test_ack_reports_decrypt_failure():
    print("\n=== 测试 11: 口令不一致 ACK 失败 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice", secret="口令A")
        node_b = make_node(tmpdir, "Bob", secret="口令B")   # 两端口令不同
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "secret.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("机密")

        ok = node_a.send_file(src)
        check("发送方 send_file 返回 False", ok is False)
        time.sleep(0.3)
        check("接收方没落盘", not os.path.exists(os.path.join(node_b.cfg.inbox, "secret.txt")))

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 12: 传输进度回调 ----------
def test_progress_callback():
    print("\n=== 测试 12: 传输进度回调 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        send_events = []
        recv_events = []
        node_a = make_node(tmpdir, "Alice")
        node_a.on_progress = lambda kind, name, done, total: send_events.append((kind, name, done, total))
        node_b = P2PNode(
            P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"), peer_name="Bob",
                      enable_mdns=False),
            on_progress=lambda kind, name, done, total: recv_events.append((kind, name, done, total)),
        )
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "big.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(512 * 1024))   # 512KB，保证有多个分块

        ok = node_a.send_file(src)
        check("发送成功", ok)
        recv = wait_for_file(node_b.cfg.inbox, "big.bin")
        check("接收成功", recv is not None)
        time.sleep(0.3)

        check("发送进度有回调", len(send_events) >= 1)
        check("发送最后一次 done==total",
              send_events[-1][2] == send_events[-1][3] == 512 * 1024)
        check("发送方向标记为 send", all(e[0] == "send" for e in send_events))
        check("接收进度有回调", len(recv_events) >= 1)
        check("接收最后一次 done==total",
              recv_events[-1][2] == recv_events[-1][3] == 512 * 1024)
        check("接收方向标记为 recv", all(e[0] == "recv" for e in recv_events))

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 13: 分块加密(WHE2)大文件往返 ----------
def test_chunked_encryption_roundtrip():
    print("\n=== 测试 13: 分块加密大文件往返 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        secret = "chunk-secret"
        node_a = make_node(tmpdir, "Alice", secret=secret)
        node_b = make_node(tmpdir, "Bob", secret=secret)
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "big_encrypted.bin")
        payload = os.urandom(1536 * 1024)   # 1.5MB 随机数据
        with open(src, "wb") as f:
            f.write(payload)

        ok = node_a.send_file(src)
        check("分块加密发送成功", ok)
        recv = wait_for_file(node_b.cfg.inbox, "big_encrypted.bin", timeout=10)
        check("接收成功", recv is not None)
        with open(recv, "rb") as f:
            check("解密后内容逐字节一致", f.read() == payload)

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 14: 分块加密的篡改/重排检测 ----------
def test_chunked_crypto_tamper():
    print("\n=== 测试 14: 分块流篡改/重排检测 ===")
    import io
    from inkhole.crypto import encrypt_chunks, ChunkedDecryptor, chunked_wire_size, CHUNK_SIZE

    secret = "tamper-secret"
    plain = os.urandom(CHUNK_SIZE + 12345)   # 两块
    blobs = list(encrypt_chunks(secret, io.BytesIO(plain)))
    check("流头 + 两帧", len(blobs) == 3)
    wire = b"".join(blobs)
    check("线上长度与预计算一致", len(wire) == chunked_wire_size(len(plain)))

    def frames(bs):
        """[(len_bytes, ct), ...]"""
        out = []
        for b in bs[1:]:
            out.append((b[:4], b[4:]))
        return out

    # 正常解密
    dec = ChunkedDecryptor(secret, blobs[0])
    got = b"".join(dec.decrypt_chunk(ct) for _, ct in frames(blobs))
    check("正常解密一致", got == plain)

    # 篡改一个字节 -> 该块解密失败
    dec2 = ChunkedDecryptor(secret, blobs[0])
    _, ct0 = frames(blobs)[0]
    bad = bytearray(ct0); bad[100] ^= 0x01
    check("篡改块被拒", dec2.decrypt_chunk(bytes(bad)) is None)

    # 交换两块顺序 -> 第一块就失败(nonce/AAD 序号不匹配)
    dec3 = ChunkedDecryptor(secret, blobs[0])
    _, ct1 = frames(blobs)[1]
    check("重排块被拒", dec3.decrypt_chunk(ct1) is None)

    # 口令不对 -> 失败
    dec4 = ChunkedDecryptor("wrong", blobs[0])
    check("错误口令被拒", dec4.decrypt_chunk(ct0) is None)


# ---------- 测试 15: 发送队列串行 + 批量聚合 ----------
def test_send_queue():
    print("\n=== 测试 15: 发送队列 ===")
    from inkhole.pet import SendQueue

    sent_order = []
    batch_results = []
    done = threading.Event()

    def fake_send(path):
        sent_order.append(path)
        time.sleep(0.05)
        return path != "b"        # b 发送失败

    q = SendQueue(fake_send,
                  on_batch_done=lambda ok, total: (batch_results.append((ok, total)),
                                                   done.set()))
    for p in ("a", "b", "c"):
        q.put(p)
    check("批量完成回调触发", done.wait(timeout=5))
    check("按放入顺序串行发送", sent_order == ["a", "b", "c"])
    check("聚合结果 2/3", batch_results == [(2, 3)])


def test_send_queue_cancel_discards_pending_items():
    """Cancelling a batch stops its active item and cleans every queued item."""
    print("\n=== 测试: 发送队列取消 ===")
    from inkhole.pet import SendQueue

    started = threading.Event()
    cancel_seen = threading.Event()
    idle = threading.Event()
    sent = []
    cleaned = []
    batches = []
    busy_states = []

    def fake_send(path):
        sent.append(path)
        started.set()
        return not cancel_seen.wait(timeout=5)

    def on_busy(busy):
        busy_states.append(busy)
        if not busy:
            idle.set()

    q = SendQueue(
        fake_send,
        on_batch_done=lambda ok, total: batches.append((ok, total)),
        on_busy_changed=on_busy,
        cancel_fn=cancel_seen.set,
    )
    for path in ("active", "queued-a", "queued-b"):
        q.put(path, on_done=lambda p, ok: cleaned.append((p, ok)))

    check("当前文件已开始", started.wait(timeout=5))
    check("取消报告有活动批次", q.cancel())
    check("发送队列回到空闲", idle.wait(timeout=5))
    check("只有当前文件进入发送函数", sent == ["active"])
    check("当前和排队文件都执行失败清理回调",
          sorted(cleaned) == sorted([
              ("active", False), ("queued-a", False), ("queued-b", False)]))
    check("取消批次不显示批量成功结果", batches == [])
    check("忙碌状态完整闭合", busy_states == [True, False])
    check("取消后队列不再忙碌", not q.busy())


def test_cancel_active_transfer_ends_both_sides():
    """Closing the outbound socket clears both progress paths and all .part data."""
    print("\n=== 测试: 活动传输取消后双方结束 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        first_receive = threading.Event()
        sender_done = threading.Event()
        receiver_done = threading.Event()
        sender_ends = []
        receiver_ends = []

        node_a = make_node(tmpdir, "Alice")
        node_b = P2PNode(
            P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"), peer_name="Bob",
                      enable_mdns=False),
            on_progress=lambda *_: (first_receive.set(), time.sleep(0.2)),
            on_transfer_end=lambda kind, name, completed: (
                receiver_ends.append((kind, name, completed)), receiver_done.set()),
        )
        node_a.on_transfer_end = lambda kind, name, completed: (
            sender_ends.append((kind, name, completed)), sender_done.set())
        node_a.start()
        node_b.start()
        time.sleep(0.2)
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "cancel.bin")
        with open(src, "wb") as f:
            f.truncate(64 * 1024 * 1024)

        result = []
        worker = threading.Thread(target=lambda: result.append(node_a.send_file(src)))
        worker.start()
        check("接收方已显示传输进度", first_receive.wait(timeout=5))
        check("取消命中当前发送", node_a.cancel_send())
        worker.join(timeout=5)
        check("发送线程及时退出", not worker.is_alive())
        check("发送函数返回失败", result == [False])
        check("发送方结束回调触发", sender_done.wait(timeout=5))
        check("接收方结束回调触发", receiver_done.wait(timeout=5))
        check("发送方结束标记为未完成",
              sender_ends == [("send", "cancel.bin", False)])
        check("接收方结束标记为未完成",
              receiver_ends == [("recv", "cancel.bin", False)])
        check("接收端未生成正式文件",
              not os.path.exists(os.path.join(node_b.cfg.inbox, "cancel.bin")))
        check("接收端未残留 .part 文件",
              not any(name.endswith(".part") for name in os.listdir(node_b.cfg.inbox)))

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 16: 仅接收目标设备(trusted_only) ----------
def test_trusted_only():
    print("\n=== 测试 16: 仅接收目标设备 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = P2PNode(P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"),
                                   peer_name="Bob", enable_mdns=False,
                                   trusted_only=True))
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "hello.txt")
        with open(src, "w") as f:
            f.write("hi")

        # B 没选中任何目标 -> 拒收 A(发送方能感知失败)
        ok = node_a.send_file(src)
        check("B 未选目标时 A 发送失败", ok is False)
        check("B 没收到文件", not os.path.exists(os.path.join(node_b.cfg.inbox, "hello.txt")))

        # B 选中 A(地址 127.0.0.1) -> 放行
        node_b._on_peer_added(
            "Alice", "127.0.0.1", node_a.actual_port,
            instance_id=node_a.cfg.instance_id,
            public_key=node_a._identity.public_key,
            identity_fingerprint=node_a._identity.fingerprint)
        node_b.select_peer("Alice")
        ok2 = node_a.send_file(src)
        check("B 选中 A 后发送成功", ok2 is True)
        recv = wait_for_file(node_b.cfg.inbox, "hello.txt")
        check("B 收到文件", recv is not None)

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 17: 多地址回退连接 ----------
def test_multi_host_fallback():
    print("\n=== 测试 17: 多地址回退 ===")
    # 环境守卫:Clash/TUN 全局代理会把保留测试网段 203.0.113.x 也"接管连通",
    # 使"不可达的第一地址"假设失效——此时跳过而不是误报回归
    try:
        socket.create_connection(("203.0.113.1", 9), timeout=0.8).close()
        print("  [SKIP] 代理/TUN 接管了保留测试网段,本测试在此环境无意义")
        return
    except OSError:
        pass
    import inkhole.p2p as p2p_mod
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    saved_timeout = p2p_mod._CONNECT_TIMEOUT
    try:
        p2p_mod._CONNECT_TIMEOUT = 1   # 让不可达地址快速超时
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        # 第一个地址不可达(TEST-NET-3 保留段)，第二个才是真的
        node_a._on_peer_added("Bob", "203.0.113.1", node_b.actual_port,
                              hosts=["203.0.113.1", "127.0.0.1"])
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "fallback.txt")
        with open(src, "w") as f:
            f.write("via second address")

        ok = node_a.send_file(src)
        check("第一地址失败后回退第二地址成功", ok is True)
        recv = wait_for_file(node_b.cfg.inbox, "fallback.txt")
        check("文件送达", recv is not None)

        node_a.stop()
        node_b.stop()
    finally:
        p2p_mod._CONNECT_TIMEOUT = saved_timeout
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 18: 幽灵设备探测剔除 ----------
def test_ghost_peer_eviction():
    """对端进程死掉(不发 mDNS goodbye)后，存活探测应把它从列表剔除。"""
    print("\n=== 测试 18: 幽灵设备探测剔除 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    node_a = node_b = None
    try:
        changed = []
        node_a = make_node(tmpdir, "Alice")
        node_a.on_peers_changed = lambda: changed.append(1)
        # 探测提速：0.2s 一轮、超时 0.5s、连续 2 轮失败才剔除
        node_a._probe_interval = 0.2
        node_a._probe_timeout = 0.5
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        time.sleep(1.0)   # 约 4~5 轮探测
        check("对端在线时探测不误杀", "Bob" in node_a.peer_names())

        changed.clear()
        # 模拟对端崩溃：直接停掉(测试模式无 mDNS，天然不会发 goodbye)
        node_b.stop()
        deadline = time.time() + 5.0
        while time.time() < deadline and "Bob" in node_a.peer_names():
            time.sleep(0.1)
        check("对端死亡后被探测剔除", "Bob" not in node_a.peer_names())
        check("选中的幽灵设备被清空", node_a.selected_peer() is None)
        check("剔除触发 on_peers_changed", len(changed) > 0)
    finally:
        for n in (node_a, node_b):
            if n:
                n.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 19: 持久化 instance_id 稳定服务名(幽灵根治) ----------
def test_persistent_instance_id():
    """同一 instance_id 重建节点应产生同一服务名；不给才随机。

    幽灵设备根因：每次启动随机 instance_id → 新服务名 → 旧记录残留。
    持久化后同一设备重启用同一 ID，去重走原地更新而非新增。
    """
    print("\n=== 测试 19: 持久化 instance_id 稳定服务名 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        # 不给 instance_id：P2PConfig 自动生成、两次不同
        c1 = P2PConfig(inbox=tmpdir, peer_name="X", enable_mdns=False)
        c2 = P2PConfig(inbox=tmpdir, peer_name="X", enable_mdns=False)
        check("未指定时自动生成 instance_id", bool(c1.instance_id))
        check("两次自动生成互不相同", c1.instance_id != c2.instance_id)

        # 指定 instance_id：原样保留
        fixed = "abcd1234" * 4
        c3 = P2PConfig(inbox=tmpdir, peer_name="X", instance_id=fixed,
                       enable_mdns=False)
        check("指定的 instance_id 原样保留", c3.instance_id == fixed)

        # 节点用 cfg 的 id；同一 id 重建 → 同一服务名(去重能命中)
        n1 = P2PNode(c3)
        n2 = P2PNode(P2PConfig(inbox=tmpdir, peer_name="X",
                               instance_id=fixed, enable_mdns=False))
        check("节点采用 cfg.instance_id", n1._instance_id == fixed)
        check("同一 id 重建服务名不变", n1._instance_id == n2._instance_id)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 20: 过滤虚拟网卡地址 ----------
def test_virtual_adapter_filtered():
    """mDNS 注册地址应过滤 VMware/VirtualBox 等虚拟网卡与 169.254 APIPA。

    Android NSD 的 host 只返回一个 IP，若拿到虚拟网卡地址(如 192.168.190.1)
    会连接失败——手机连不到电脑的真实局域网地址。
    """
    print("\n=== 测试 20: 过滤虚拟网卡地址 ===")
    import types
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    node = None
    try:
        node = make_node(tmpdir, "Alice")

        # 构造假的 psutil.net_if_addrs()：WiFi + VMware + APIPA 混合
        class FakeAddr:
            def __init__(self, family, address):
                self.family = family
                self.address = address

        fake_ifaces = {
            "WLAN": [FakeAddr(socket.AF_INET, "192.168.5.7")],
            "VMware Network Adapter VMnet1": [FakeAddr(socket.AF_INET, "192.168.190.1")],
            "VMware Network Adapter VMnet8": [FakeAddr(socket.AF_INET, "192.168.110.1")],
            "以太网 2": [FakeAddr(socket.AF_INET, "169.254.215.246")],  # APIPA
        }
        fake_psutil = types.SimpleNamespace(net_if_addrs=lambda: fake_ifaces)

        # 注入假 psutil，强制默认路由 IP 为 WiFi 地址
        import sys as _sys
        orig_psutil = _sys.modules.get("psutil")
        _sys.modules["psutil"] = fake_psutil
        node._get_local_ip = lambda: "192.168.5.7"
        try:
            ips = node._get_local_ips()
        finally:
            if orig_psutil is not None:
                _sys.modules["psutil"] = orig_psutil
            else:
                _sys.modules.pop("psutil", None)

        check("保留真实 WiFi 地址", "192.168.5.7" in ips)
        check("过滤 VMware VMnet1", "192.168.190.1" not in ips)
        check("过滤 VMware VMnet8", "192.168.110.1" not in ips)
        check("过滤 169.254 APIPA 地址", "169.254.215.246" not in ips)
        check("默认路由地址排最前", ips[0] == "192.168.5.7")
    finally:
        if node:
            node.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 21: 智能保留选中目标 ----------
def test_smart_keep_selection():
    """选中目标离线后重新上线(service_name 匹配)应自动恢复选中。"""
    print("\n=== 测试 21: 智能保留选中目标 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    node = None
    try:
        node = make_node(tmpdir, "Alice")
        svc = "Bob-abc123._inkhole._tcp.local."
        node._on_peer_added("Bob", "127.0.0.1", 5000, service_name=svc)
        node.select_peer("Bob")
        check("初始选中 Bob", node.selected_peer() == "Bob")

        # Bob 离线：当前选择清空，但记住 service_name
        node._on_peer_removed("Bob")
        check("离线后当前选择清空", node.selected_peer() is None)
        check("记住离线前 service_name", node._last_selected_service == svc)

        # Bob 重新上线(同 service_name，IP/端口变了)：自动恢复选中
        node._on_peer_added("Bob", "127.0.0.1", 5001, service_name=svc)
        check("重新上线自动恢复选中", node.selected_peer() == "Bob")

        # 另一台设备上线不应误恢复
        node.select_peer(None)
        node._on_peer_added("Carol", "127.0.0.1", 5002,
                            service_name="Carol-xyz._inkhole._tcp.local.")
        check("不同设备上线不误恢复", node.selected_peer() is None)
    finally:
        if node:
            node.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 22: 待机唤醒/换网后 mDNS 重建 ----------
def test_mdns_rebuild():
    """待机唤醒/换网后 _rebuild_mdns 应重建 mDNS 层：换全新 Zeroconf 实例、
    重新注册服务，且不动 TCP 监听端口。enable_mdns=False 时应为空操作。"""
    print("\n=== 测试 22: 待机唤醒/换网后 mDNS 重建 ===")
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        print("  [SKIP] 未安装 zeroconf，跳过重建测试")
        return

    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    node = None
    try:
        # 真实 mDNS 节点(唯一名字，避免与局域网其他墨洞混淆)
        inbox = os.path.join(tmpdir, "rebuild_inbox")
        cfg = P2PConfig(inbox=inbox, listen_port=0,
                        peer_name="RebuildTest-" + uuid.uuid4().hex[:6],
                        enable_mdns=True)
        node = P2PNode(cfg)
        node.start()
        time.sleep(0.5)

        zc1 = node._zc
        port1 = node._actual_port
        check("启动后 Zeroconf 实例已建", zc1 is not None)
        check("启动后已注册服务", node._service_info is not None)
        check("启动后记录了本机 IP", len(node._last_local_ips) > 0)
        check("TCP 监听端口已分配", port1 > 0)

        # 模拟唤醒/换网触发重建
        node._rebuild_mdns("测试唤醒")
        time.sleep(0.5)

        check("重建后换了全新 Zeroconf 实例", node._zc is not None and node._zc is not zc1)
        check("重建后服务重新注册", node._service_info is not None)
        check("重建不动 TCP 监听端口", node._actual_port == port1)
        check("重建后仍记录本机 IP", len(node._last_local_ips) > 0)
    finally:
        if node:
            node.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)

    # enable_mdns=False 时重建应为空操作(守卫生效，不误建 Zeroconf)
    tmpdir2 = tempfile.mkdtemp(prefix="inkhole_test_")
    node2 = None
    try:
        node2 = make_node(tmpdir2, "NoMdns")   # make_node 内 enable_mdns=False
        node2.start()
        node2._rebuild_mdns("测试")
        check("enable_mdns=False 时重建不建 Zeroconf", node2._zc is None)
    finally:
        if node2:
            node2.stop()
        shutil.rmtree(tmpdir2, ignore_errors=True)


# ---------- 测试: 同名文件不覆盖(加后缀) ----------
def test_no_overwrite_adds_suffix():
    print("\n=== 测试: 同名文件不覆盖，加 (2) 后缀 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        # 收件箱预置一个同名文件，模拟"已收到过"
        os.makedirs(node_b.cfg.inbox, exist_ok=True)
        existing = os.path.join(node_b.cfg.inbox, "dup.txt")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("原有内容")

        src = os.path.join(tmpdir, "dup.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("新发来的内容")
        check("发送成功", node_a.send_file(src))

        got = wait_for_file(node_b.cfg.inbox, "dup (2).txt")
        check("同名文件落为 dup (2).txt", got is not None)
        with open(existing, encoding="utf-8") as f:
            check("原有文件未被覆盖", f.read() == "原有内容")
    finally:
        node_a.stop(); node_b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试: 非法文件名清洗 ----------
def test_illegal_filename_sanitized():
    print("\n=== 测试: 非法文件名清洗 ===")
    from inkhole.p2p import _safe_filename
    check("裁掉正斜杠路径", _safe_filename("a/b/c.txt") == "c.txt")
    check("裁掉反斜杠路径", _safe_filename("a\\b\\c.txt") == "c.txt")
    check("替换 NTFS 非法字符", _safe_filename('a:b*c?.txt') == "a_b_c_.txt")
    check("去掉尾部点和空格", _safe_filename("name.  ") == "name")
    check("纯路径穿越回退 unknown", _safe_filename("../..") == "unknown")
    check("空名回退 unknown", _safe_filename("") == "unknown")


# ---------- 测试: 目录打包成 zip ----------
def test_zip_dir_roundtrip():
    print("\n=== 测试: 目录打包 zip 往返 ===")
    import zipfile
    from inkhole.p2p import _zip_dir
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        # 造一个带子目录、中文名、空文件的源目录
        src = os.path.join(tmpdir, "我的资料")
        os.makedirs(os.path.join(src, "子目录"))
        with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as f:
            f.write("内容A")
        with open(os.path.join(src, "子目录", "b.txt"), "w", encoding="utf-8") as f:
            f.write("内容B")
        open(os.path.join(src, "空.dat"), "w").close()

        zip_path = _zip_dir(src)
        check("返回的是 .zip 路径", zip_path.endswith(".zip"))
        check("zip 文件名是目录名", os.path.basename(zip_path) == "我的资料.zip")
        check("zip 文件已生成", os.path.isfile(zip_path))

        # 解压回来逐一核对
        out = os.path.join(tmpdir, "out")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(out)
        with open(os.path.join(out, "a.txt"), encoding="utf-8") as f:
            check("a.txt 内容一致", f.read() == "内容A")
        with open(os.path.join(out, "子目录", "b.txt"), encoding="utf-8") as f:
            check("子目录/b.txt 内容一致", f.read() == "内容B")
        check("空文件保留", os.path.isfile(os.path.join(out, "空.dat")))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试: 空目录打包不报错 ----------
def test_zip_dir_empty():
    print("\n=== 测试: 空目录打包 ===")
    import zipfile
    from inkhole.p2p import _zip_dir
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        src = os.path.join(tmpdir, "空目录")
        os.makedirs(src)
        zip_path = _zip_dir(src)
        check("空目录也生成合法 zip", os.path.isfile(zip_path))
        with zipfile.ZipFile(zip_path) as z:
            check("zip 内无文件条目", len([n for n in z.namelist() if not n.endswith("/")]) == 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试: 打包保留空子目录 ----------
def test_zip_dir_preserves_empty_subdir():
    print("\n=== 测试: 打包保留空子目录 ===")
    import zipfile
    from inkhole.p2p import _zip_dir
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        src = os.path.join(tmpdir, "带空目录")
        os.makedirs(os.path.join(src, "空子目录"))
        with open(os.path.join(src, "有内容.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        zip_path = _zip_dir(src)
        out = os.path.join(tmpdir, "out")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(out)
        check("空子目录被保留", os.path.isdir(os.path.join(out, "空子目录")))
        check("有内容文件保留", os.path.isfile(os.path.join(out, "有内容.txt")))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试: 文件夹流式发送端到端 ----------
def test_folder_send_end_to_end():
    print("\n=== 测试: 文件夹发送端到端 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start(); node_b.start()
        time.sleep(0.3)
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "项目")
        os.makedirs(os.path.join(src, "src"))
        os.makedirs(os.path.join(src, "空目录"))
        with open(os.path.join(src, "readme.txt"), "w", encoding="utf-8") as f:
            f.write("hello folder")
        with open(os.path.join(src, "src", "main.py"), "w", encoding="utf-8") as f:
            f.write("print('hello')\n")

        check("流式发送文件夹成功", node_a.send_path(src))

        got = wait_for_directory(node_b.cfg.inbox, "项目")
        check("对端直接收到可用目录", got is not None)
        check("没有生成 zip", not os.path.exists(os.path.join(node_b.cfg.inbox, "项目.zip")))
        check("空子目录被保留", os.path.isdir(os.path.join(got, "空目录")))
        with open(os.path.join(got, "readme.txt"), encoding="utf-8") as f:
            check("根目录文件内容一致", f.read() == "hello folder")
        with open(os.path.join(got, "src", "main.py"), encoding="utf-8") as f:
            check("嵌套文件内容一致", f.read() == "print('hello')\n")

        check("再次发送同名目录成功", node_a.send_path(src))
        check("同名目录不会覆盖", wait_for_directory(node_b.cfg.inbox, "项目 (2)") is not None)
    finally:
        node_a.stop(); node_b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_encrypted_folder_stream():
    print("\n=== 测试: 加密文件夹强制 WHE2 流式发送 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice", secret="folder-secret")
        node_b = make_node(tmpdir, "Bob", secret="folder-secret")
        node_a.start(); node_b.start()
        time.sleep(0.3)
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")

        src = os.path.join(tmpdir, "机密项目")
        os.makedirs(src)
        with open(os.path.join(src, "tiny.txt"), "w", encoding="utf-8") as f:
            f.write("small folder still uses chunked encryption")

        check("小文件夹加密发送成功", node_a.send_path(src))

        got = wait_for_directory(node_b.cfg.inbox, "机密项目")
        check("加密文件夹直接落为目录", got is not None)
        with open(os.path.join(got, "tiny.txt"), encoding="utf-8") as f:
            check("加密文件夹内容一致", f.read() == "small folder still uses chunked encryption")
    finally:
        node_a.stop(); node_b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_folder_legacy_zip_fallback():
    print("\n=== 测试: 旧客户端文件夹 ZIP 回退 ===")
    import zipfile
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "LegacyBob")
        node_a.start(); node_b.start()
        time.sleep(0.3)
        node_a._on_peer_added(
            "LegacyBob", "127.0.0.1", node_b.actual_port,
            instance_id=node_b.cfg.instance_id,
            capabilities={"reliable-v3"},
            public_key=node_b._identity.public_key,
            identity_fingerprint=node_b._identity.fingerprint)
        node_a.select_peer("LegacyBob")
        node_a._probe_peer_capabilities = lambda _peer: set()

        src = os.path.join(tmpdir, "旧版兼容")
        os.makedirs(src)
        with open(os.path.join(src, "note.txt"), "w", encoding="utf-8") as f:
            f.write("legacy")
        check("旧客户端回退发送成功", node_a.send_path(src))
        got = wait_for_file(node_b.cfg.inbox, "旧版兼容.zip")
        check("旧客户端收到 zip", got is not None)
        with zipfile.ZipFile(got) as archive:
            check("回退 zip 保留内容", archive.read("note.txt") == b"legacy")
    finally:
        node_a.stop(); node_b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_encrypted_folder_ack_reports_failure():
    print("\n=== 测试: 加密文件夹失败回执 ===")
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice", secret="sender-secret")
        node_b = make_node(tmpdir, "Bob", secret="receiver-secret")
        node_a.start(); node_b.start()
        time.sleep(0.3)
        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)
        node_a.select_peer("Bob")
        src = os.path.join(tmpdir, "wrong-key")
        os.makedirs(src)
        with open(os.path.join(src, "data.bin"), "wb") as f:
            f.write(os.urandom(1024))

        check("口令不一致时发送方返回失败", node_a.send_path(src) is False)
        check("口令不一致不落正式目录", not os.path.exists(
            os.path.join(node_b.cfg.inbox, "wrong-key")))
        check("口令不一致不残留暂存目录", not any(
            name.endswith(".folder.part") for name in os.listdir(node_b.cfg.inbox)))
    finally:
        node_a.stop(); node_b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_folder_traversal_rejected_atomically():
    print("\n=== 测试: WHF1 路径穿越被原子拒收 ===")
    from inkhole.p2p import _ACK_FAIL, _FOLDER_ENTRY, _FOLDER_KIND, _FOLDER_MAGIC
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    node = make_node(tmpdir, "Receiver")
    try:
        node.start()
        time.sleep(0.2)
        relative = b"../escape.txt"
        payload = (_FOLDER_MAGIC + struct.pack("!I", 1)
                   + _FOLDER_ENTRY.pack(1, len(relative), 4, 0)
                   + relative + b"evil")
        with socket.create_connection(("127.0.0.1", node.actual_port), timeout=3) as sock:
            check("接收方返回失败回执", send_v3_payload(
                sock, "unsafe-folder", payload, kind=_FOLDER_KIND) == _ACK_FAIL)

        check("越界文件未写入", not os.path.exists(os.path.join(tmpdir, "escape.txt")))
        check("正式目录未落盘", not os.path.exists(
            os.path.join(node.cfg.inbox, "unsafe-folder")))
        check("隐藏暂存目录已清理", not any(
            name.endswith(".folder.part") for name in os.listdir(node.cfg.inbox)))
    finally:
        node.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试: 发送队列 per-item 清理(连拖文件夹无竞态) ----------
def test_send_queue_per_item_cleanup():
    """临时 zip 改为「按文件发送完成即清理」——回归防线(评审 Important)。

    旧实现用共享 _pending_zip_dirs 列表 + 批次末无差别 rmtree,连拖多个文件夹
    时存在竞态:worker 发完第一批触发清理的瞬间,可能把第二个文件夹刚入队、
    尚未发送的 zip 提前删掉(静默不送达),或因 append/重绑并发导致临时目录
    永不清理(磁盘泄漏)。

    新语义:清理精确绑定单个文件——SendQueue.put(path, on_done=cb),worker
    每发完一个文件即调 cb(path, ok),由回调删除该文件自己的临时目录。无跨批次
    耦合、无共享可变列表。

    本测试构造真实 SendQueue(不依赖 Qt/GUI),用 3 个真实临时目录(镜像
    _zip_dir 的 inkhole_zip_xxx/<name>.zip 结构),每个 zip 各带自己的 on_done
    清理闭包,断言:
      - 3 个 zip 发送函数都被调用(都送达/都尝试,无提前删除导致的漏发);
      - 发送时每个 zip 都还在磁盘上(证明没被别的批次提前 rmtree);
      - 3 个临时目录最终都不存在(各清各的,无泄漏);
      - 其中一个发送失败的文件夹,其临时目录同样被清理(失败不泄漏)。
    """
    print("\n=== 测试: 发送队列 per-item 清理(连拖文件夹无竞态) ===")
    from inkhole.pet import SendQueue

    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    try:
        # 造 3 个独立临时目录,各放一个假 zip(镜像 _zip_dir 输出结构)
        temp_dirs, zips = [], []
        for i in range(3):
            d = os.path.join(tmpdir, f"zipdir_{i}")
            os.makedirs(d)
            z = os.path.join(d, f"folder_{i}.zip")
            with open(z, "wb") as f:
                f.write(b"PK\x03\x04fake")
            temp_dirs.append(d)
            zips.append(z)

        sent = []
        existed_at_send = {}
        lock = threading.Lock()
        done = threading.Event()
        remaining = {"n": len(zips)}
        fail_zip = zips[2]        # folder_2 发送失败:验证失败也清理(不泄漏)

        def fake_send(path):
            # 发送时文件必须还在——若已被别的批次提前 rmtree,这里会记 False
            existed_at_send[path] = os.path.isfile(path)
            sent.append(path)
            time.sleep(0.02)
            return path != fail_zip

        def make_on_done(temp_dir):
            def _cb(path, ok):
                shutil.rmtree(temp_dir, ignore_errors=True)   # 各清各的
                with lock:
                    remaining["n"] -= 1
                    if remaining["n"] == 0:
                        done.set()
            return _cb

        q = SendQueue(fake_send)
        for z, d in zip(zips, temp_dirs):
            q.put(z, on_done=make_on_done(d))

        check("所有文件夹都处理完(per-item 回调触发)", done.wait(timeout=5))
        check("发送函数对 3 个 zip 都被调用(无提前删除致漏发)",
              sorted(sent) == sorted(zips))
        check("发送时每个 zip 都还在磁盘(无提前删除)",
              all(existed_at_send.get(z) for z in zips))
        for i, d in enumerate(temp_dirs):
            check(f"临时目录 {i} 已清理(无泄漏)", not os.path.exists(d))
        check("发送失败的文件夹临时目录也被清理(失败不泄漏)",
              not os.path.exists(temp_dirs[2]))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 主入口 ----------
if __name__ == "__main__":
    _tests = [
        test_direct_transfer,
        test_encrypted_transfer,
        test_peer_selection,
        test_peer_offline,
        test_callbacks,
        test_multiple_files,
        test_path_traversal,
        test_partial_transfer_not_delivered,
        test_malicious_size_rejected,
        test_duplicate_names_and_service_removal,
        test_ack_reports_decrypt_failure,
        test_progress_callback,
        test_chunked_encryption_roundtrip,
        test_chunked_crypto_tamper,
        test_send_queue,
        test_send_queue_cancel_discards_pending_items,
        test_cancel_active_transfer_ends_both_sides,
        test_trusted_only,
        test_multi_host_fallback,
        test_ghost_peer_eviction,
        test_persistent_instance_id,
        test_virtual_adapter_filtered,
        test_smart_keep_selection,
        test_mdns_rebuild,
        test_no_overwrite_adds_suffix,
        test_illegal_filename_sanitized,
        test_zip_dir_roundtrip,
        test_zip_dir_empty,
        test_zip_dir_preserves_empty_subdir,
        test_folder_send_end_to_end,
        test_encrypted_folder_stream,
        test_folder_legacy_zip_fallback,
        test_encrypted_folder_ack_reports_failure,
        test_folder_traversal_rejected_atomically,
        test_send_queue_per_item_cleanup,
    ]
    for _t in _tests:
        try:
            _t()
        except AssertionError:
            pass    # 已计数并打印 FAIL，继续跑下一组
        except Exception as e:
            _failed += 1
            print(f"  [FAIL] {_t.__name__} 异常: {e}")

    print(f"\n{'='*40}")
    print(f"  通过: {_passed}  失败: {_failed}")
    print(f"{'='*40}")
    sys.exit(0 if _failed == 0 else 1)
