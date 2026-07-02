#!/usr/bin/env python3
"""
test_p2p.py
===========
虫洞 P2P 引擎端到端测试。

测试覆盖：
  1. TCP 直连传输（绕过 mDNS，手动注册对端）
  2. 端到端加密传输
  3. 自动选择首个对端
  4. 切换目标设备
  5. mDNS 自动发现（本地回环，可能受系统防火墙影响）
  6. 多文件连续发送

运行：PYTHONPATH=src python3 tests/test_p2p.py
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

from pyftp_server.wormhole.p2p import P2PNode, P2PConfig, PeerInfo, _MAGIC


# ---------- 测试框架(与 test_ftp.py 风格一致) ----------
_passed = 0
_failed = 0

def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


# ---------- 工具 ----------
def make_node(tmpdir, name="test", secret="", port=0):
    """创建一个 P2P 节点，收件箱在 tmpdir 下。"""
    inbox = os.path.join(tmpdir, name + "_inbox")
    cfg = P2PConfig(inbox=inbox, listen_port=port, peer_name=name, secret=secret)
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
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
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

        # 自动选中首个对端
        check("Alice 自动选中 Bob", node_a.selected_peer() == "Bob")

        # 发送文件
        src = os.path.join(tmpdir, "hello.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Hello from Alice!\n你好，虫洞！")

        ok = node_a.send_file(src)
        check("send_file 返回 True", ok)

        # 等待 Bob 收到
        recv = wait_for_file(node_b.cfg.inbox, "hello.txt")
        check("Bob 收到文件", recv is not None)
        if recv:
            with open(recv, "r", encoding="utf-8") as f:
                content = f.read()
            check("文件内容一致", content == "Hello from Alice!\n你好，虫洞！")

        node_a.stop()
        node_b.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 2: 端到端加密 ----------
def test_encrypted_transfer():
    print("\n=== 测试 2: 端到端加密传输 ===")
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
    try:
        secret = "my-secret-passphrase"
        node_a = make_node(tmpdir, "Alice", secret=secret)
        node_b = make_node(tmpdir, "Bob", secret=secret)
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)

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
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
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
        check("自动选中第一个(Bob)", node_a.selected_peer() == "Bob")

        # 切换到 Carol
        node_a.select_peer("Carol")
        check("切换到 Carol", node_a.selected_peer() == "Carol")

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


# ---------- 测试 4: 对端离线自动切换 ----------
def test_peer_offline():
    print("\n=== 测试 4: 对端离线自动切换 ===")
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
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

        check("选中 Bob", node_a.selected_peer() == "Bob")

        # Bob 离线
        node_a._on_peer_removed("Bob")
        check("Bob 离线后自动切到 Carol", node_a.selected_peer() == "Carol")
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
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
    try:
        received_files = []
        sent_files = []
        status_msgs = []

        node_a = make_node(tmpdir, "Alice")
        node_b = P2PNode(
            P2PConfig(inbox=os.path.join(tmpdir, "Bob_inbox"), peer_name="Bob"),
            on_received=lambda p: received_files.append(p),
            on_status=lambda s: status_msgs.append(s),
        )
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)

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
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_b = make_node(tmpdir, "Bob")
        node_a.start()
        node_b.start()
        time.sleep(0.3)

        node_a._on_peer_added("Bob", "127.0.0.1", node_b.actual_port)

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


# ---------- 测试 7: 暂停/恢复 ----------
def test_pause():
    print("\n=== 测试 7: 暂停/恢复 ===")
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
    try:
        node_a = make_node(tmpdir, "Alice")
        node_a.start()
        time.sleep(0.3)

        check("初始未暂停", not node_a.is_paused())

        node_a.set_paused(True)
        check("暂停后 is_paused=True", node_a.is_paused())

        node_a.set_paused(False)
        check("恢复后 is_paused=False", not node_a.is_paused())

        node_a.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 测试 8: 路径穿越防御 ----------
def test_path_traversal():
    print("\n=== 测试 8: 路径穿越防御 ===")
    tmpdir = tempfile.mkdtemp(prefix="wormhole_test_")
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


# ---------- 主入口 ----------
if __name__ == "__main__":
    test_direct_transfer()
    test_encrypted_transfer()
    test_peer_selection()
    test_peer_offline()
    test_callbacks()
    test_multiple_files()
    test_pause()
    test_path_traversal()

    print(f"\n{'='*40}")
    print(f"  通过: {_passed}  失败: {_failed}")
    print(f"{'='*40}")
    sys.exit(0 if _failed == 0 else 1)
