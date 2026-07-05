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
