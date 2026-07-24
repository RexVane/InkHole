"""Desktop client for the shared InkHole transport-core sidecar."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Callable


class TransportCoreError(RuntimeError):
    pass


def _binary_name() -> str:
    return "inkhole-core.exe" if sys.platform == "win32" else "inkhole-core"


def find_core_binary() -> str | None:
    override = os.environ.get("INKHOLE_CORE_PATH", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.extend([
            Path(bundled) / _binary_name(),
            Path(bundled) / "inkhole" / _binary_name(),
        ])
    package = Path(__file__).resolve()
    candidates.append(package.parents[2] / "transport-core" / "bin" / _binary_name())
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _core_environment() -> dict[str, str]:
    """Pass desktop proxy settings to the Go sidecar without persisting them."""
    environment = os.environ.copy()
    if environment.get("INKHOLE_PROXY_URL", "").strip():
        return environment
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    proxy = str(proxies.get("https") or proxies.get("http") or "").strip()
    if proxy.startswith(("http://", "https://")):
        environment["INKHOLE_PROXY_URL"] = proxy
    return environment


class TransportCore:
    """Thread-safe request/response client with asynchronous event delivery."""

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        binary = find_core_binary()
        if not binary:
            raise TransportCoreError("跨网核心未安装，请重新构建或安装墨洞")
        startup = {}
        if sys.platform == "win32":
            startup["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                [binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                bufsize=1, env=_core_environment(), **startup)
        except OSError as exc:
            raise TransportCoreError(f"无法启动跨网核心：{exc}") from exc
        self._event_callback = on_event
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="inkhole-core-reader")
        self._reader.start()
        # 延长启动 ping 超时至 10 秒，避免慢速机器上 Go 进程初始化时间过长导致失败
        self.call("ping", timeout=10)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        process = self._process
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        self._fail_pending("跨网核心已停止")

    def call(self, method: str, params: dict | None = None,
             timeout: float = 30) -> dict:
        if self._closed.is_set() or self._process.poll() is not None:
            raise TransportCoreError("跨网核心未运行")
        request_id = uuid.uuid4().hex
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        line = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._write_lock:
                if not self._process.stdin:
                    raise BrokenPipeError
                self._process.stdin.write(line + "\n")
                self._process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TransportCoreError("跨网核心连接已断开") from exc
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TransportCoreError(f"跨网操作超时：{method}") from exc
        if not response.get("ok"):
            raise TransportCoreError(str(response.get("error") or "跨网操作失败"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _read_loop(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            return
        try:
            for raw in stdout:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                request_id = str(message.get("id") or "")
                if request_id:
                    with self._pending_lock:
                        waiter = self._pending.pop(request_id, None)
                    if waiter:
                        waiter.put(message)
                elif message.get("event") and self._event_callback:
                    try:
                        self._event_callback(message)
                    except Exception:
                        pass
        finally:
            self._closed.set()
            self._fail_pending("跨网核心意外退出")

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        failure = {"ok": False, "error": message}
        for waiter in waiters:
            try:
                waiter.put_nowait(failure)
            except queue.Full:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
