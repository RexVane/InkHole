"""Small OS credential-store wrapper; never falls back to plaintext secrets."""

from __future__ import annotations

import queue
import threading

_SERVICE = "com.rexvane.inkhole"
_GET_TIMEOUT_SECONDS = 3.0


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def available() -> bool:
    backend = _keyring()
    if backend is None:
        return False
    try:
        return backend.get_keyring().priority > 0
    except Exception:
        return False


def get_with_status(name: str) -> tuple[bool, str]:
    """Return (read_completed, value) without allowing a Keychain prompt to hang startup."""
    backend = _keyring()
    if backend is None:
        return False, ""
    result: queue.Queue[tuple[bool, str]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put((True, str(backend.get_password(_SERVICE, name) or "")))
        except Exception:
            result.put((False, ""))

    threading.Thread(target=worker, daemon=True).start()
    try:
        return result.get(timeout=_GET_TIMEOUT_SECONDS)
    except queue.Empty:
        return False, ""


def get(name: str) -> str:
    ok, value = get_with_status(name)
    return value if ok else ""


def set(name: str, value: str) -> bool:
    backend = _keyring()
    if backend is None:
        return False
    try:
        if value:
            backend.set_password(_SERVICE, name, value)
        else:
            try:
                backend.delete_password(_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


def delete(name: str) -> None:
    set(name, "")
