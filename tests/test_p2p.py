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

# 把 src 加入 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from inkhole.p2p import P2PNode, P2PConfig, PeerInfo, _MAGIC


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
def make_node(tmpdir, name="test", secret="", port=0):
    """创建一个 P2P 节点，收件箱在 tmpdir 下。

    enable_mdns=False：只起 TCP 层，手动注册对端。测试不碰真实 mDNS，
    否则测试节点会互相发现、局域网里真实运行的墨洞也会污染结果。
    """
    inbox = os.path.join(tmpdir, name + "_inbox")
    cfg = P2PConfig(inbox=inbox, listen_port=port, peer_name=name,
                    secret=secret, enable_mdns=False)
    return P2PNode(cfg)


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
        header = json.dumps({
            "filename": "../../../evil.txt",
            "size": 4,
            "encrypted": False,
        }).encode("utf-8")
        sock.sendall(_MAGIC)
        sock.sendall(struct.pack("!I", len(header)))
        sock.sendall(header)
        sock.sendall(b"evil")
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
        node_b = P2PNode(
            P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"), peer_name="Bob",
                      enable_mdns=False),
            on_received=lambda p: received.append(p),
        )
        node_b.start()
        time.sleep(0.3)

        # 收件箱里已有同名完整文件——半截文件绝不能把它覆盖掉
        existing = os.path.join(node_b.cfg.inbox, "report.txt")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("完整的旧文件")

        # 声明 100 字节但只发 50 字节就断开(模拟发送方中途崩溃/断网)
        sock = socket.create_connection(("127.0.0.1", node_b.actual_port), timeout=5)
        header = json.dumps({"filename": "report.txt", "size": 100,
                             "encrypted": False}).encode("utf-8")
        sock.sendall(_MAGIC)
        sock.sendall(struct.pack("!I", len(header)))
        sock.sendall(header)
        sock.sendall(b"x" * 50)
        sock.close()
        time.sleep(0.8)

        with open(existing, "r", encoding="utf-8") as f:
            content = f.read()
        check("旧文件未被半截文件覆盖", content == "完整的旧文件")
        check("on_received 未触发", len(received) == 0)
        check(".part 残留已清理", not os.path.exists(existing + ".part"))

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
    import inkhole.p2p as p2p_mod
    tmpdir = tempfile.mkdtemp(prefix="inkhole_test_")
    saved_threshold = p2p_mod._CHUNK_ENC_THRESHOLD
    try:
        # 把分块阈值压到 256KB，1.5MB 文件即可触发 chunked 路径
        p2p_mod._CHUNK_ENC_THRESHOLD = 256 * 1024
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
        p2p_mod._CHUNK_ENC_THRESHOLD = saved_threshold
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
        node_b._on_peer_added("Alice", "127.0.0.1", node_a.actual_port)
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
        fixed = "abcd1234"
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
