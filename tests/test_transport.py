import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from inkhole import secret_store
from inkhole import transport


class _QueueReader:
    def __init__(self):
        self._lines = queue.Queue()

    def put(self, message):
        self._lines.put(json.dumps(message) + "\n")

    def close(self):
        self._lines.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        line = self._lines.get(timeout=5)
        if line is None:
            raise StopIteration
        return line


class _FakeProcess:
    def __init__(self):
        self.stdout = _QueueReader()
        self.stdin = self
        self._returncode = None
        self._timers = []

    def write(self, line):
        request = json.loads(line)
        method = request["method"]
        params = request.get("params") or {}

        def respond():
            if method == "emit-test-event":
                self.stdout.put({"event": "test.event", "data": params})
            self.stdout.put({
                "id": request["id"],
                "ok": True,
                "result": {
                    "protocol": 1,
                    "value": params.get("value"),
                },
            })

        timer = threading.Timer(float(params.get("delay_ms", 0)) / 1000, respond)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()
        return len(line)

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0
        self.stdout.close()

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.terminate()


def test_find_core_binary_prefers_explicit_executable(monkeypatch, tmp_path):
    binary = tmp_path / ("inkhole-core.exe" if os.name == "nt" else "inkhole-core")
    binary.write_bytes(b"placeholder")
    binary.chmod(0o700)
    monkeypatch.setenv("INKHOLE_CORE_PATH", str(binary))

    assert transport.find_core_binary() == str(binary)


def test_transport_core_matches_concurrent_responses_and_events(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(transport, "find_core_binary", lambda: "/fake/core")
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *_a, **_kw: process)
    events = []

    with transport.TransportCore(events.append) as core:
        values = list(range(16))

        def invoke(value):
            return core.call("echo", {
                "value": value,
                "delay_ms": (len(values) - value) * 2,
            })["value"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(invoke, values)) == values
        core.call("emit-test-event", {"value": "ready"})
        deadline = threading.Event()
        for _attempt in range(100):
            if events:
                break
            deadline.wait(0.005)

    assert events == [{"event": "test.event", "data": {"value": "ready"}}]


def test_transport_core_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(transport, "find_core_binary", lambda: None)
    with pytest.raises(transport.TransportCoreError, match="跨网核心未安装"):
        transport.TransportCore()


class _CredentialBackend:
    def __init__(self, priority=1):
        self.priority = priority
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


class _KeyringModule:
    def __init__(self, backend):
        self.backend = backend

    def get_keyring(self):
        return self.backend

    def get_password(self, service, name):
        return self.backend.get_password(service, name)

    def set_password(self, service, name, value):
        return self.backend.set_password(service, name, value)

    def delete_password(self, service, name):
        return self.backend.delete_password(service, name)


def test_secret_store_uses_credential_backend(monkeypatch):
    backend = _CredentialBackend()
    monkeypatch.setattr(secret_store, "_keyring", lambda: _KeyringModule(backend))

    assert secret_store.available()
    assert secret_store.set("ssh:test:private_key", "secret-key")
    assert secret_store.get("ssh:test:private_key") == "secret-key"
    secret_store.delete("ssh:test:private_key")
    assert secret_store.get("ssh:test:private_key") == ""


def test_secret_store_never_falls_back_when_backend_is_unavailable(monkeypatch):
    monkeypatch.setattr(secret_store, "_keyring", lambda: None)

    assert not secret_store.available()
    assert not secret_store.set("ssh:test:private_key", "must-not-persist")
    assert secret_store.get("ssh:test:private_key") == ""


def test_secret_store_read_times_out_without_overwriting(monkeypatch):
    class SlowBackend:
        def get_password(self, _service, _name):
            time.sleep(0.1)
            return "late-secret"

    monkeypatch.setattr(secret_store, "_keyring", lambda: _KeyringModule(SlowBackend()))
    monkeypatch.setattr(secret_store, "_GET_TIMEOUT_SECONDS", 0.01)

    assert secret_store.get_with_status("lan_identity_p256") == (False, "")


def test_secret_store_allows_longer_background_read(monkeypatch):
    class SlowBackend:
        def get_password(self, _service, _name):
            time.sleep(0.02)
            return "stored-secret"

    monkeypatch.setattr(secret_store, "_keyring", lambda: _KeyringModule(SlowBackend()))
    monkeypatch.setattr(secret_store, "_GET_TIMEOUT_SECONDS", 0.001)

    assert secret_store.get_with_status(
        "ssh:test:noise_private", timeout_seconds=0.1) == (True, "stored-secret")
