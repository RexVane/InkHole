"""
pet.py
======
桌宠墨洞挂件(PySide6 + QML) — P2P 局域网直连模式，无需服务器。

形态：黑洞吞噬感 —— 中心深邃黑点 + 乳白色吸积盘/光晕，向内吸卷旋转；
      桌面小图标大小，低调浮在角落，无边框、透明、置顶、可拖动。

交互：
  - 从桌面拖文件到挂件上 -> 黑洞播放发送动画 -> P2P 直连发给目标设备。
  - 收到对端文件(node.on_received 回调) -> 黑洞播放接收动画(文件已落在收件箱)。
  - 右键菜单：发送目标 / 打开收件箱 / 更换收件箱 / 开机自启 / 状态 / 退出。
  - 鼠标拖动窗口可挪到桌面任意位置。

后端：复用 p2p.P2PNode(mDNS 发现 + TCP 直连)。本文件只负责"面子"(动画/拖拽)，
      "里子"(传输)全交给 P2P 引擎。两层解耦，P2P 引擎已通过自动化测试。

运行(需在有图形界面的机器上，先 pip install PySide6 zeroconf)：
  PYTHONPATH=src python3 -m inkhole.pet
  PYTHONPATH=src python3 -m inkhole.pet --name 我的电脑
  PYTHONPATH=src python3 -m inkhole.pet --secret 加密口令
"""

from __future__ import annotations
import os
import re
import sys
import json
import hmac
import queue
import argparse
import copy
import secrets
import shutil
import threading
import time
from collections import deque

from .p2p import P2PNode, P2PConfig
from .device_identity import DeviceIdentity
from .transport import TransportCore, TransportCoreError
from . import secret_store
from . import __version__ as _APP_VERSION


def _version_newer(remote: str, local: str) -> bool:
    """语义化比较:remote 是否比 local 新。容忍 v 前缀与位数不齐。"""
    def parts(v: str) -> tuple:
        v = v.strip().lstrip("vV")
        out = []
        for seg in v.split("."):
            digits = "".join(ch for ch in seg if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out + [0] * (4 - len(out)))
    try:
        return parts(remote) > parts(local)
    except Exception:
        return False


def _parse_release_checksum(raw: bytes, filename: str) -> str:
    """Parse a plain checksum or the signed PowerShell manifest first line."""
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("更新校验文件不是 ASCII") from exc
    line = lines[0].strip() if lines else ""
    prefix = "# INKHOLE-SHA256 "
    if line.startswith(prefix):
        line = line[len(prefix):]
    fields = line.split()
    if (len(fields) != 2 or fields[1].lstrip("*") != filename
            or re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]) is None):
        raise ValueError("更新校验文件格式无效")
    return fields[0].lower()


def _sha256_path(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(256 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _extract_update_zip(zip_path: str, destination: str) -> None:
    """Extract an update only when every ZIP member remains below destination."""
    import stat
    import zipfile
    root = os.path.abspath(destination)
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > 20_000:
            raise ValueError("更新包文件数量异常")
        if sum(member.file_size for member in members) > 1024 * 1024 * 1024:
            raise ValueError("更新包解压大小异常")
        for member in members:
            target = os.path.abspath(os.path.join(root, member.filename))
            try:
                inside = os.path.commonpath((root, target)) == root
            except ValueError:
                inside = False
            if not inside:
                raise ValueError("更新包包含越界路径")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("更新包包含符号链接")
            if member.flag_bits & 0x1:
                raise ValueError("更新包包含加密条目")
        archive.extractall(root)


def _verify_windows_update_signature(current_exe: str, candidate_exe: str,
                                     manifest_path: str, workdir: str) -> None:
    """Require the candidate EXE and whole-package manifest to match our signer."""
    import subprocess
    script = os.path.join(workdir, "verify-update.ps1")
    with open(script, "w", encoding="utf-8") as output:
        output.write(
            "param([string]$Current, [string]$Candidate, [string]$Manifest)\n"
            "$currentSig = Get-AuthenticodeSignature -LiteralPath $Current\n"
            "$candidateSig = Get-AuthenticodeSignature -LiteralPath $Candidate\n"
            "$manifestSig = Get-AuthenticodeSignature -LiteralPath $Manifest\n"
            "@{ current_status = $currentSig.Status.ToString(); "
            "current_thumbprint = $currentSig.SignerCertificate.Thumbprint; "
            "candidate_status = $candidateSig.Status.ToString(); "
            "candidate_thumbprint = $candidateSig.SignerCertificate.Thumbprint; "
            "manifest_status = $manifestSig.Status.ToString(); "
            "manifest_thumbprint = $manifestSig.SignerCertificate.Thumbprint } | "
            "ConvertTo-Json -Compress\n"
        )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", script, current_exe, candidate_exe, manifest_path],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("无法验证更新程序的 Windows 签名")
    try:
        signature = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise ValueError("Windows 签名验证结果无效") from exc
    current_thumbprint = str(signature.get("current_thumbprint") or "").upper()
    candidate_thumbprint = str(signature.get("candidate_thumbprint") or "").upper()
    manifest_thumbprint = str(signature.get("manifest_thumbprint") or "").upper()
    if (signature.get("current_status") != "Valid"
            or signature.get("candidate_status") != "Valid"
            or signature.get("manifest_status") != "Valid"
            or not current_thumbprint
            or not hmac.compare_digest(current_thumbprint, candidate_thumbprint)
            or not hmac.compare_digest(current_thumbprint, manifest_thumbprint)):
        raise ValueError("更新包签名无效或发布证书与当前版本不一致")


def _windows_update_script(app_dir: str, backup_dir: str, unzip_dir: str,
                           zip_path: str, manifest_path: str) -> str:
    """Build a recoverable onedir replacement script for the detached updater."""
    return "\r\n".join([
        "@echo off",
        "timeout /t 2 /nobreak >nul",
        f'robocopy "{app_dir}" "{backup_dir}" /MIR /R:2 /W:1 >nul',
        "if errorlevel 8 goto restart_current",
        f'robocopy "{unzip_dir}\\InkHolePet" "{app_dir}" /MIR /IS /IT /R:3 /W:2 >nul',
        "if errorlevel 8 goto rollback",
        f'start "" "{app_dir}\\InkHolePet.exe"',
        "goto cleanup",
        ":rollback",
        f'robocopy "{backup_dir}" "{app_dir}" /MIR /IS /IT /R:3 /W:2 >nul',
        "if errorlevel 8 goto rollback_failed",
        ":restart_current",
        f'start "" "{app_dir}\\InkHolePet.exe"',
        ":cleanup",
        f'rd /s /q "{backup_dir}"',
        f'rd /s /q "{unzip_dir}"',
        f'del "{zip_path}" "{manifest_path}"',
        'del "%~f0"',
        "exit /b 0",
        ":rollback_failed",
        f'start "" "{app_dir}\\InkHolePet.exe"',
        "rem Keep the verified package and backup for manual recovery.",
        "exit /b 1",
    ])


def _summarize_release_notes(raw: str, max_items: int = 4) -> str:
    """把 GitHub Release 的 Markdown 压缩成更新弹窗里的简短要点。

    与安卓端 Updater.summarizeReleaseNotes 行为一致:去图片/链接语法、列表前缀、
    强调标记与反引号;遇到「安装/下载/校验」等标题段落即停止;裸 URL 行丢弃;
    单条超 90 字截断。每条前缀「• 」,最多 max_items 条。
    """
    items: list[str] = []
    stop_sections = ("安装", "下载", "校验", "install", "download", "verify", "checksum")
    for raw_line in raw.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if items and any(s in heading for s in stop_sections):
                break
            continue
        if line.startswith(">"):
            continue

        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"^[-*+]\s+", "", text)
        text = re.sub(r"^\d+[.)]\s+", "", text)
        text = text.replace("**", "").replace("__", "").replace("`", "").strip()
        if not text or text.startswith("http://") or text.startswith("https://"):
            continue
        if len(text) > 90:
            text = text[:89].rstrip() + "…"
        items.append(f"• {text}")
        if len(items) >= max_items:
            break
    return "\n".join(items)


_RELEASES_PAGE = "https://github.com/RexVane/InkHole/releases/latest"
_RELEASES_API = "https://api.github.com/repos/RexVane/InkHole/releases/latest"
_REPOSITORY_PAGE = "https://github.com/RexVane/InkHole"

def _qml_path() -> str:
    """定位 inkhole.qml。

    源码运行时它就在本模块同级目录；被 PyInstaller 打包成单文件后,数据文件
    会解压到临时目录 sys._MEIPASS,需按打包时的相对路径(inkhole/)
    去那里找。两种环境都覆盖,打包/源码运行同一份代码。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = os.path.join(base, "inkhole", "inkhole.qml")
        if os.path.exists(bundled):
            return bundled
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "inkhole.qml")


_QML_FILE = _qml_path()


def _windows_desktop() -> str:
    """Windows 真实桌面目录：经 SHGetKnownFolderPath 查询。

    桌面可能被 OneDrive/域策略重定向到任意位置，不能假设 ~/Desktop
    或 ~/OneDrive/Desktop。查询失败回退 ~/Desktop。
    """
    try:
        import ctypes
        from ctypes import wintypes
        # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        guid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                     (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        path_ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)) == 0:
            desktop = path_ptr.value
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            if desktop:
                return desktop
    except Exception:
        pass
    return os.path.expanduser(os.path.join("~", "Desktop"))


def _default_inbox() -> str:
    """默认收件箱目录,按平台给出常用位置(均可被 --inbox 覆盖)。

    Windows: <真实桌面>/inkhole (桌面可能被 OneDrive 重定向,动态查询)
    macOS:   ~/Documents/inkhole
    其他:    ~/InkHole/收件箱
    """
    if sys.platform == "win32":
        return os.path.join(_windows_desktop(), "inkhole")
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Documents", "inkhole"))
    return os.path.expanduser(os.path.join("~", "InkHole", "收件箱"))


# ---------- 设置持久化 ----------
# 双击 exe 的用户没有命令行：名字/收件箱等写普通配置，口令写系统凭据库。
# 显式 CLI 参数 > 已保存设置 > 默认值；显式参数会在可用时安全保存。

_CONFIG_LOCK = threading.RLock()
_TRANSFER_SECRET_NAME = "transfer_secret_v1"
_TRANSFER_SECRET_WARNING = ""

def _config_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "InkHole", "config.json")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/InkHole/config.json")
    return os.path.expanduser("~/.config/inkhole/config.json")


def _load_saved_config() -> dict:
    with _CONFIG_LOCK:
        try:
            with open(_config_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def _save_config(cfg: P2PConfig, **extra) -> None:
    """写回配置。读改写合并：不丢掉 P2PConfig 之外的界面项(如 show_pet)。"""
    path = _config_path()
    with _CONFIG_LOCK:
        try:
            data = _load_saved_config()
            # Only remove obsolete pre-1.5 relay keys. cross_network is the
            # additive v1.5 schema and is preserved by the read-modify-write.
            for stale in ("relay", "transport_mode"):
                data.pop(stale, None)
            # Secrets live in the OS credential store. Also scrub the legacy
            # plaintext field whenever any setting is persisted.
            data.pop("secret", None)
            data.update({"name": cfg.peer_name,
                         "encryption_enabled": bool(cfg.encryption_enabled),
                         "inbox": cfg.inbox, "port": cfg.listen_port,
                         "inbox_auto_classify": bool(cfg.inbox_auto_classify),
                         "inbox_category_dirs": dict(cfg.inbox_category_dirs or {}),
                         "trusted_only": cfg.trusted_only,
                         "instance_id": cfg.instance_id,
                         "manual_peers": list(cfg.manual_peers or []),
                         "trusted_peers": dict(cfg.trusted_peers or {})})
            data.update(extra)
            data.pop("secret", None)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            pass


def _normalize_cross_network(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    wormhole = source.get("wormhole") if isinstance(source.get("wormhole"), dict) else {}
    ssh_raw = source.get("ssh") if isinstance(source.get("ssh"), dict) else {}
    profile_raw = ssh_raw.get("profile") if isinstance(ssh_raw.get("profile"), dict) else {}

    def normalized_port(value, default: int, allow_zero: bool = False) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if allow_zero and port == 0:
            return 0
        return port if 1 <= port <= 65535 else default

    profile_id = str(profile_raw.get("id") or "").strip() or secrets.token_hex(12)
    profile = {
        "id": profile_id,
        "host": str(profile_raw.get("host") or "").strip(),
        "port": normalized_port(profile_raw.get("port") or 22, 22),
        "user": str(profile_raw.get("user") or "").strip(),
        "private_key_mode": ("paste" if profile_raw.get("private_key_mode") == "paste"
                             else "file"),
        "private_key_path": str(profile_raw.get("private_key_path") or ""),
        "private_key_label": str(profile_raw.get("private_key_label") or ""),
        "host_key_sha256": str(profile_raw.get("host_key_sha256") or ""),
    }
    peers = []
    for value in ssh_raw.get("peers") or []:
        if not isinstance(value, dict):
            continue
        try:
            remote_port = int(value.get("remote_port") or 0)
            instance_id = str(value.get("instance_id") or "").strip()
            public_key = str(value.get("noise_public") or "").strip()
            if not instance_id or not public_key or not 1 <= remote_port <= 65535:
                continue
            peers.append({
                "id": str(value.get("id") or instance_id),
                "name": str(value.get("name") or "SSH 设备"),
                "instance_id": instance_id,
                "remote_port": remote_port,
                "noise_public": public_key,
                "end_to_end": bool(value.get("end_to_end", True)),
            })
        except (TypeError, ValueError):
            continue
    return {
        "wormhole": {
            "rendezvous_url": str(wormhole.get("rendezvous_url") or "").strip(),
            "transit_relay": str(wormhole.get("transit_relay") or "").strip(),
        },
        "ssh": {
            "enabled": bool(ssh_raw.get("enabled", False)),
            "profile": profile,
            "remote_port": normalized_port(
                ssh_raw.get("remote_port") or 0, 0, allow_zero=True),
            "peers": peers,
        },
    }


def _summarize_transfer_paths(paths: list[str]) -> dict:
    """Validate selected paths and build the metadata shown before receiving."""
    item_count = 0
    file_count = 0
    directory_count = 0
    total_bytes = 0
    names = []
    normalized = []
    for raw in paths:
        path = os.path.abspath(str(raw))
        if not (os.path.isfile(path) or os.path.isdir(path)):
            continue
        normalized.append(path)
        item_count += 1
        if len(names) < 8:
            names.append(os.path.basename(path) or path)
        if os.path.isfile(path):
            file_count += 1
            total_bytes += os.path.getsize(path)
            continue
        directory_count += 1
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            if root != path:
                directory_count += 1
            for filename in files:
                current = os.path.join(root, filename)
                if os.path.isfile(current) and not os.path.islink(current):
                    file_count += 1
                    total_bytes += os.path.getsize(current)
    if not normalized:
        raise ValueError("没有可发送的文件或文件夹")
    return {
        "paths": normalized,
        "summary": {
            "device_name": "",
            "instance_id": "",
            "item_count": item_count,
            "file_count": file_count,
            "directory_count": directory_count,
            "total_bytes": total_bytes,
            "names": names,
        },
    }


def _ssh_secret_name(profile_id: str, kind: str) -> str:
    return f"ssh:{profile_id}:{kind}"


class SendQueue:
    """串行发送队列：一次拖 N 个文件不再开 N 个并发连接互踩。

    单工作线程按序发送；一批(队列清空)结束后回调 on_batch_done(成功数, 总数)，
    多文件时给用户一个聚合结果。文件与文件夹都只传原始路径，协议工作线程
    决定采用 folder-v1 流式传输还是 ZIP 回退，因此不会阻塞 GUI。
    纯标准库实现，不依赖 Qt，可单测。
    """

    def __init__(self, send_fn, on_batch_done=None, on_busy_changed=None,
                 cancel_fn=None):
        self._send = send_fn
        self._on_batch_done = on_batch_done
        self._on_busy_changed = on_busy_changed
        self._cancel_current = cancel_fn
        # generation 让 cancel() 能同时覆盖「刚出队、尚未开始」的竞态窗口。
        self._q: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self._lock = threading.Lock()
        self._generation = 0
        self._active_generation: int | None = None
        self._batch_total = 0
        self._batch_ok = 0
        self._working = False
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def put(self, path: str, on_done=None) -> None:
        """把一个文件排入发送队列。

        on_done(path, ok)：该文件发送结束后在工作线程内回调一次(无论成败)，
        用于「发完即清理」它自己的临时资源(如目录打包产生的临时 zip 目录)。
        """
        notify = False
        with self._lock:
            if self._batch_total == 0 and not self._working:
                notify = True
            self._batch_total += 1
            self._q.put((self._generation, path, on_done))
        if notify and self._on_busy_changed:
            self._on_busy_changed(True)

    def busy(self) -> bool:
        with self._lock:
            return self._working or self._batch_total > 0

    def cancel_requested(self) -> bool:
        """Whether cancel() invalidated the item currently inside send_fn."""
        with self._lock:
            return (self._active_generation is not None and
                    self._active_generation != self._generation)

    def cancel(self) -> bool:
        """Cancel the active file and discard every queued file in this batch."""
        pending = []
        with self._lock:
            busy_before = self._working or self._batch_total > 0
            old_generation = self._generation
            self._generation += 1
            active = self._active_generation == old_generation
            self._batch_total = 0
            self._batch_ok = 0
            while True:
                try:
                    pending.append(self._q.get_nowait())
                except queue.Empty:
                    break

        for _generation, path, on_done in pending:
            if on_done is not None:
                try:
                    on_done(path, False)
                except Exception:
                    pass
            self._q.task_done()

        if active and self._cancel_current is not None:
            try:
                self._cancel_current()
            except Exception:
                pass
        elif busy_before and self._cancel_current is not None:
            # Covers cancellation while send_file is entering its active state.
            try:
                self._cancel_current()
            except Exception:
                pass

        if busy_before and not active and self._on_busy_changed:
            self._on_busy_changed(False)
        return busy_before

    def _loop(self) -> None:
        while True:
            generation, path, on_done = self._q.get()
            with self._lock:
                stale = generation != self._generation
                if not stale:
                    self._working = True
                    self._active_generation = generation
            if stale:
                if on_done is not None:
                    try:
                        on_done(path, False)
                    except Exception:
                        pass
                self._q.task_done()
                continue
            ok = False
            try:
                ok = bool(self._send(path))
            except Exception:
                pass
            # per-item 清理：谁的文件发完就清谁的临时资源，成败都清(失败不泄漏)。
            # 在工作线程内、发送完成之后调用——绝不会提前删掉尚未发送的文件。
            if on_done is not None:
                try:
                    on_done(path, ok)
                except Exception:
                    pass
            batch_done = None
            notify_idle = False
            with self._lock:
                cancelled = generation != self._generation
                self._active_generation = None
                self._working = False
                if cancelled:
                    notify_idle = self._q.empty() and self._batch_total == 0
                else:
                    if ok:
                        self._batch_ok += 1
                    if self._q.empty():
                        batch_done = (self._batch_ok, self._batch_total)
                        self._batch_ok = 0
                        self._batch_total = 0
                        notify_idle = True
            self._q.task_done()
            if batch_done and self._on_batch_done:
                try:
                    self._on_batch_done(*batch_done)
                except Exception:
                    pass
            if notify_idle and self._on_busy_changed:
                try:
                    self._on_busy_changed(False)
                except Exception:
                    pass


def _build_config(argv=None):
    global _TRANSFER_SECRET_WARNING
    _TRANSFER_SECRET_WARNING = ""
    saved = _load_saved_config()
    ap = argparse.ArgumentParser(description="墨洞桌宠挂件(P2P 局域网直连，无需服务器)")
    ap.add_argument("--inbox", default=None,
                    help="收件箱目录(收到的文件放这;默认随平台,改一次会记住)")
    ap.add_argument("--port", type=int, default=None,
                    help="P2P 监听端口(0=操作系统自动分配)")
    ap.add_argument("--name", default=None,
                    help="本机显示名(默认主机名；右键菜单里对端看到的就是这个名字)")
    ap.add_argument("--secret", default=None,
                    help="端到端加密口令(两台设备必须一致；需 cryptography 库)")
    ap.add_argument("--size", type=int, default=0,
                    help="挂件边长像素(0=随屏幕自适应，约为系统图标基准的 1.5 倍)")
    args = ap.parse_args(argv)

    inbox = args.inbox if args.inbox is not None else str(saved.get("inbox") or "") or _default_inbox()
    try:
        port = args.port if args.port is not None else int(saved.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    name = args.name if args.name is not None else str(saved.get("name") or "")

    # Device identity is required for every WHPC/WHPP connection. Load it
    # before optional passwords so an authorization wait for an old password
    # cannot queue ahead of creating a fresh identity on this installation.
    identity_read_ok, stored_identity = secret_store.get_with_status(
        "lan_identity_p256")
    try:
        identity = (DeviceIdentity.from_private_key(stored_identity)
                    if identity_read_ok and stored_identity else None)
    except (ValueError, TypeError):
        identity = None
    if identity is None:
        identity = DeviceIdentity.generate()
        if identity_read_ok:
            if not secret_store.set(
                    "lan_identity_p256", identity.export_private_key()):
                _TRANSFER_SECRET_WARNING = (
                    "无法保存设备身份，本次运行使用临时身份")
        else:
            _TRANSFER_SECRET_WARNING = (
                "系统安全存储正在等待授权，本次运行使用临时设备身份")

    secure_read_ok, secure_secret = secret_store.get_with_status(
        _TRANSFER_SECRET_NAME)
    has_legacy_secret = "secret" in saved
    legacy_secret = str(saved.get("secret") or "")
    if args.secret is not None:
        secret = args.secret
        if not secret_store.set(_TRANSFER_SECRET_NAME, secret):
            _TRANSFER_SECRET_WARNING = (
                "无法使用系统安全存储，传输口令仅在本次运行中生效")
    elif secure_read_ok and secure_secret:
        secret = secure_secret
    else:
        secret = legacy_secret
        if legacy_secret and not secure_read_ok:
            _TRANSFER_SECRET_WARNING = (
                "系统安全存储正在等待授权；已从普通配置移除旧传输口令，"
                "口令仅在本次运行中生效")
        elif legacy_secret and not secret_store.set(
                _TRANSFER_SECRET_NAME, legacy_secret):
            _TRANSFER_SECRET_WARNING = (
                "无法迁移旧传输口令到系统安全存储；"
                "已从普通配置移除，口令仅在本次运行中生效")
    saved_encryption = saved.get("encryption_enabled")
    encryption_enabled = (
        bool(saved_encryption) if isinstance(saved_encryption, bool)
        else bool(secret))
    if args.secret is not None:
        # An explicit --secret (including an empty value) expresses the CLI intent.
        encryption_enabled = bool(secret)
    elif encryption_enabled and not secret:
        encryption_enabled = False
    trusted_only = bool(saved.get("trusted_only", False))
    inbox_auto_classify = bool(saved.get("inbox_auto_classify", False))
    inbox_category_dirs = (saved.get("inbox_category_dirs")
                           if isinstance(saved.get("inbox_category_dirs"), dict)
                           else {})
    instance_id = str(saved.get("instance_id") or "")   # 空则 P2PConfig 自动生成
    trusted_peers = (saved.get("trusted_peers")
                     if isinstance(saved.get("trusted_peers"), dict) else {})
    manual_peers = []
    for m in (saved.get("manual_peers") or []):
        try:
            m_host = str(m.get("host", "")).strip()
            m_port = int(m.get("port", 0))
            m_name = str(m.get("name", "")).strip()
            if m_host and 1 <= m_port <= 65535:
                entry = {"name": m_name, "host": m_host, "port": m_port}
                m_instance = str(m.get("instance_id") or "").lower()
                if (len(m_instance) == 32
                        and all(ch in "0123456789abcdef" for ch in m_instance)):
                    entry["instance_id"] = m_instance
                manual_peers.append(entry)
        except (TypeError, ValueError, AttributeError):
            continue   # 配置文件被手改坏的条目直接丢弃

    cfg = P2PConfig(inbox=inbox, listen_port=port, peer_name=name, secret=secret,
                    inbox_auto_classify=inbox_auto_classify,
                    inbox_category_dirs=inbox_category_dirs,
                    trusted_only=trusted_only, instance_id=instance_id,
                    manual_peers=manual_peers,
                    encryption_enabled=encryption_enabled,
                    core_ingress_token=secrets.token_urlsafe(24),
                    identity_private_key=identity.export_private_key(),
                    trusted_peers=trusted_peers)
    # 首次运行(配置里还没有 instance_id)时生成一个并落盘，之后重启复用同一 ID
    if (str(saved.get("instance_id") or "").lower() != cfg.instance_id
            or has_legacy_secret
            or (bool(saved_encryption) and not encryption_enabled)):
        _save_config(cfg)
    if any(a is not None for a in (args.inbox, args.port, args.name, args.secret)):
        _save_config(cfg)   # 显式 CLI 参数视为用户意图，记住
    return cfg, args.size


def _adaptive_pet_size(override: int) -> int:
    """挂件边长 = 系统程序图标尺寸 × 1.5。
    override>0 时直接用该值；否则在 macOS 上读取 Dock 实际图标尺寸
    (com.apple.dock tilesize)作为基准——用户把 Dock 图标调大，挂件随之变大。
    读不到(或非 macOS)则回退到常见图标基准 64。"""
    if override > 0:
        return override
    icon_base = 64
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(["defaults", "read", "com.apple.dock", "tilesize"],
                                 capture_output=True, text=True, timeout=3)
            icon_base = int(float(out.stdout.strip()))
        except Exception:
            icon_base = 64
    return round(icon_base * 1.5)


def _install_crash_log(inbox: str) -> str:
    """让 GUI 版"莫名自己退出"可排查 + 兜底修复打包后的致命陷阱。

    打包成 windowed exe(spec 里 console=False)后,sys.stdout/sys.stderr 会变成
    None;此时任何 print() 都会抛异常,而后台线程每次状态变化/重连都 print,
    异常逐层上抛会直接打死同步线程——这是 exe 版"自己断掉"的元凶之一。这里把空的
    stdout/stderr 兜底重定向到日志文件,并记录未捕获异常,既消除崩溃源又留下现场。
    源码运行时 stdout 正常,不会被替换。返回日志路径。"""
    import time
    import traceback
    try:
        os.makedirs(inbox, exist_ok=True)
    except OSError:
        pass
    log_path = os.path.join(inbox, "inkhole-pet.log")
    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return log_path
    # 仅在为 None(windowed 打包)时替换,避免影响源码运行时的真实控制台
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file

    def _hook(exc_type, exc, tb):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 未捕获异常:\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass

    sys.excepthook = _hook    # 槽函数里漏出的异常改为记日志,尽量不让进程直接终止
    return log_path


# ---------- 开机自启 ----------
# 注意:名字不能用 "InkHolePet"/"WormholePet" —— 这两个已被联想电脑管家
# 记入启动项黑名单(搬进 Run\LenovoDisabled 并加 rem| 前缀禁用)。
# 换成管家未收录的名字才能稳定保留在 Run 键。
_APP_NAME = "InkHole"


def _src_dir() -> str:
    """src 目录绝对路径(pet.py 上两级)。"""
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _startup_script_path() -> str:
    """开机自启脚本/配置文件路径(跨平台)。"""
    if sys.platform == "win32":
        return os.path.join(os.path.expanduser("~"), "inkhole-startup.bat")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/LaunchAgents/com.rexvane.inkhole-pet.plist")
    return os.path.expanduser("~/.config/autostart/inkhole-pet.desktop")


def is_autostart_enabled() -> bool:
    """检查当前是否已设置开机自启。"""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, _APP_NAME)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False
    return os.path.exists(_startup_script_path())


def set_autostart(enabled: bool, cfg: P2PConfig) -> bool:
    """设置或取消开机自启，返回操作后的状态。

    自启项不带任何参数：普通设置从 config.json 读取，传输口令从系统凭据库读取，
    不会明文进入配置、注册表或自启脚本。
    """
    path = _startup_script_path()
    if enabled:
        src = _src_dir()
        proj = os.path.dirname(src)
        python = sys.executable
        frozen = getattr(sys, "frozen", False)

        if sys.platform == "win32":
            try:
                if frozen:
                    # 打包 exe：注册表直接指向 exe
                    cmd = f'"{python}"'
                else:
                    # 源码运行：生成 .bat 脚本
                    content = "\r\n".join([
                        "@echo off",
                        f'cd /d "{proj}"',
                        f'set "PYTHONPATH={src}"',
                        f'"{python}" -m inkhole.pet',
                    ])
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    cmd = f'"{path}"'

                # 写注册表
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     r"Software\Microsoft\Windows\CurrentVersion\Run",
                                     0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
                winreg.CloseKey(key)
            except Exception as e:
                print(f"[ERROR] 设置开机自启失败: {e}")
                import traceback
                traceback.print_exc()
                return False

        elif sys.platform == "darwin":
            try:
                if frozen:
                    exec_path = sys.executable  # .app 内的可执行文件
                    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rexvane.inkhole-pet</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exec_path}</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>"""
                else:
                    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rexvane.inkhole-pet</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-c</string>
        <string>import os,sys;os.chdir({proj!r});sys.path.insert(0,{src!r});from inkhole.pet import main;main()</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>WorkingDirectory</key><string>{proj}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key><string>{src}</string>
    </dict>
</dict>
</plist>"""
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"[ERROR] 设置开机自启失败: {e}")
                import traceback
                traceback.print_exc()
                return False

        else:
            # Linux: .desktop
            try:
                if frozen:
                    exec_line = f'"{python}"'
                else:
                    exec_line = f'sh -c \'cd "{proj}" && PYTHONPATH="{src}" "{python}" -m inkhole.pet\''
                content = f"""[Desktop Entry]
Type=Application
Name=墨洞桌宠
Exec={exec_line}
Terminal=false
X-GNOME-Autostart-enabled=true"""
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"[ERROR] 设置开机自启失败: {e}")
                import traceback
                traceback.print_exc()
                return False

    else:
        # 取消自启
        if sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     r"Software\Microsoft\Windows\CurrentVersion\Run",
                                     0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(key, _APP_NAME)
                winreg.CloseKey(key)
            except (FileNotFoundError, OSError) as e:
                print(f"[INFO] 注册表项不存在或删除失败: {e}")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"[ERROR] 删除启动脚本失败: {e}")

    result = is_autostart_enabled()
    print(f"[INFO] 开机自启设置{'成功' if result == enabled else '失败'}: enabled={enabled}, result={result}")
    return result


def main(argv=None) -> None:
    # Source launches run inside Homebrew Python.app, whose English-only bundle
    # metadata otherwise makes native macOS panels ignore the system language.
    # This must happen before importing PySide6/QApplication initializes Cocoa.
    from .macos import configure_bundle_localizations
    configure_bundle_localizations()

    try:
        from PySide6.QtCore import QObject, Signal, Slot, QUrl, Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from .branding import (MACOS_ICON_SCALE,
                               make_app_icon as _make_app_icon)
        # 托盘菜单需要 QtWidgets(QApplication/QSystemTrayIcon/QMenu);
        # 不可用时降级:仍能跑桌宠,只是没有系统托盘(见 _setup_tray 的容错)
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
            _HAS_WIDGETS = True
        except ImportError:
            _HAS_WIDGETS = False
    except ImportError:
        sys.stderr.write(
            "未安装 PySide6。请先运行：pip install PySide6 --break-system-packages\n"
            "(P2P 引擎本身无需 GUI，可用 python -m inkhole.p2p 跑命令行版)\n")
        raise SystemExit(1)

    # Keychain may display an access prompt after an ad-hoc local rebuild.
    # Cocoa must already have an application/event owner or SecItemCopyMatching
    # can wait forever before the first window and P2P listener are created.
    app = (QApplication if _HAS_WIDGETS else QGuiApplication)([sys.argv[0]])
    cfg, size_override = _build_config(argv)
    _install_crash_log(cfg.inbox)   # 尽早安装:之后任何崩溃/print 都安全且留痕

    def _setup_tray(app, bridge):
        """构建右键菜单 +(可用时)系统托盘图标。

        托盘与桌宠各有一份菜单,尾部动作语义不同(用户明确要求):
          桌宠右键 → 「关闭桌宠」= 只收起挂件(等于设置里关掉开关),程序继续跑;
                     另保留「退出程序」。
          托盘     → 「退出」= 退出整个应用。
        发送目标子菜单在 aboutToShow 时动态重建——对端随时上下线。
        """
        if not _HAS_WIDGETS:
            return None

        def _fill_menu(menu, from_pet):
            try:
                menu.clear()

                # 发送目标子菜单
                peers = bridge.node.peers()
                peer_menu = menu.addMenu("发送目标")
                if not peers:
                    act = peer_menu.addAction("（等待发现设备…）")
                    act.setEnabled(False)
                else:
                    selected = bridge.node.selected_peer()
                    for peer in peers:
                        # 显示设备名-实例 ID 短后缀；完整 ID 仅用于协议身份校验
                        marker = "●" if peer.name == selected else "○"
                        suffix = f"-{peer.instance_id[:8]}" if peer.instance_id else ""
                        label = f"{marker} {peer.name}{suffix}"
                        act = peer_menu.addAction(label)
                        act.setCheckable(True)
                        act.setChecked(peer.name == selected)
                        # lambda 默认绑定技巧:用 name=peer.name 固定当前值
                        act.triggered.connect(
                            lambda checked=False, name=peer.name: bridge._select_peer(name))
                    peer_menu.addSeparator()
                    act_none = peer_menu.addAction("○ 不选目标")
                    act_none.setCheckable(True)
                    act_none.setChecked(selected is None)
                    act_none.triggered.connect(
                        lambda checked=False: bridge._select_peer(None))

                menu.addSeparator()

                act_main = menu.addAction("打开主界面")
                act_main.triggered.connect(bridge.showMain)

                act_open = menu.addAction("打开收件箱")
                act_open.triggered.connect(bridge.openInbox)

                menu.addSeparator()

                if from_pet:
                    act_hide = menu.addAction("关闭桌宠")
                    act_hide.triggered.connect(bridge.hidePet)
                    act_quit = menu.addAction("退出程序")
                else:
                    act_quit = menu.addAction("退出")
                act_quit.triggered.connect(bridge.quit)
            except Exception as e:
                print(f"[ERROR] 菜单构建失败: {e}")
                import traceback
                traceback.print_exc()

        tray_menu = QMenu()
        tray_menu.aboutToShow.connect(lambda: _fill_menu(tray_menu, False))
        pet_menu = QMenu()
        pet_menu.aboutToShow.connect(lambda: _fill_menu(pet_menu, True))
        bridge._tray_menu = pet_menu   # 桌宠右键弹出的是桌宠版菜单

        # 仅当系统托盘可用时才创建并显示托盘图标;不可用也不影响右键菜单
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(_make_app_icon(), app)
        tray.setToolTip("墨洞")
        tray.setContextMenu(tray_menu)
        tray.activated.connect(
            lambda reason: bridge.showMain()
            if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        return tray

    # ---- Python<->QML 桥：把 P2P 引擎的事件转成 QML 信号驱动动画 ----
    class Bridge(QObject):
        absorb = Signal(str)          # 通知 QML 播放发送动画(参数=文件名)
        emit_out = Signal(str)        # 通知 QML 播放接收动画(参数=文件名)
        status = Signal(str)          # 临时状态文字(2.2s 后消失)
        peersChanged = Signal()       # 设备列表变化(刷新菜单)
        recentChanged = Signal()      # 接收历史变化(不触发桌宠接收动画)
        errorState = Signal(str)      # 错误信息(持续显示，非空=有错误，空=清除)
        progress = Signal(str, int)   # 传输进度(kind "send"/"recv", 百分比 0-100)
        progressCleared = Signal()
        transferStateChanged = Signal(bool)
        sendStateChanged = Signal(bool)
        transportEvent = Signal(str, object)
        # 检查更新结果: has_new, 最新版本号, 更新说明摘要, 资产下载 url(空=无对应平台包)
        updateCheckFinished = Signal(bool, str, str, str)

        def __init__(self, cfg: P2PConfig):
            super().__init__()
            self._tray_menu = None        # 由 _setup_tray 注入:桌宠右键时弹出
            self._main_window = None      # 由 main() 注入:主界面窗口
            saved_recent = _load_saved_config().get("recent_files", [])
            if not isinstance(saved_recent, list):
                saved_recent = []
            self._recent: deque[str] = deque(
                (str(path) for path in saved_recent[:50]
                 if isinstance(path, str)),
                maxlen=50)
            self._engine_lock = threading.RLock()
            self._progress_lock = threading.Lock()
            self._progress_generation = 0
            self._progress_active = False
            self._progress_key = None
            self._speed_state = {}
            self._last_status = "正在启动…"
            self._lan_cfg = cfg
            self._cross_network = _normalize_cross_network(
                _load_saved_config().get("cross_network"))
            self._transport_core = None
            self._transport_error = ""
            self._wormhole_pending: dict[str, list[str]] = {}
            self._wormhole_active = ""
            self._ssh_session_id = ""
            self._ssh_runtime_peers: dict[str, dict] = {}
            self.transportEvent.connect(self._handle_transport_event)
            # 设置保存会后台重启节点(mDNS 重新注册,阻塞数秒不能占 UI 线程);
            # _restart_gate 保证同一时刻只有一次重启,_restarting 供发送路径守卫。
            self._restart_gate = threading.Lock()
            self._restarting = False
            self.node = self._make_node(cfg)
            # 串行发送队列：拖一堆文件不再开一堆并发连接
            self._sendq = SendQueue(
                lambda p: self.node.send_path(
                    p, should_cancel=self._sendq.cancel_requested),
                on_batch_done=lambda ok, total: self._on_batch_done(ok, total),
                on_busy_changed=self._on_send_busy_changed,
                cancel_fn=lambda: self.node.cancel_send(),
            )
            self.node.start()
            self._start_transport_core()

        def _make_node(self, cfg):
            return P2PNode(
                cfg,
                on_sent=lambda n: self.absorb.emit(n),
                on_received=lambda p: self._on_received_file(p),
                on_status=lambda s: self._route_status(s),
                on_peers_changed=lambda: self.peersChanged.emit(),
                on_progress=lambda kind, name, done, total: self._on_progress(
                    kind, name, done, total),
                on_transfer_end=lambda kind, name, completed: self._on_transfer_end(
                    kind, name, completed),
                on_manual_peer_verified=lambda: _save_config(self._lan_cfg),
                on_trust_changed=lambda: _save_config(self._lan_cfg),
            )

        # ---------- shared cross-network transport core ----------
        def _start_transport_core(self) -> None:
            try:
                core = TransportCore(lambda message: self.transportEvent.emit(
                    str(message.get("event") or ""), message.get("data") or {}))
                core.call("start", {
                    "local_target": f"127.0.0.1:{self.node.actual_port}",
                    "local_token": self._lan_cfg.core_ingress_token,
                    "device_name": self._lan_cfg.peer_name,
                    "instance_id": self._lan_cfg.instance_id,
                }, timeout=8)
                self._transport_core = core
                self._transport_error = ""
                if self._cross_network["ssh"]["enabled"]:
                    threading.Thread(target=self._start_ssh_runtime, daemon=True).start()
            except (TransportCoreError, OSError) as exc:
                self._transport_core = None
                self._transport_error = str(exc)

        def _retarget_transport_core(self) -> None:
            core = self._transport_core
            if core is None:
                return
            try:
                core.call("start", {
                    "local_target": f"127.0.0.1:{self.node.actual_port}",
                    "local_token": self._lan_cfg.core_ingress_token,
                    "device_name": self._lan_cfg.peer_name,
                    "instance_id": self._lan_cfg.instance_id,
                }, timeout=8)
            except TransportCoreError as exc:
                self.transportEvent.emit("core.error", {"error": str(exc)})

        @Slot(str, object)
        def _handle_transport_event(self, name: str, data) -> None:
            if not isinstance(data, dict):
                data = {}
            if name == "wormhole.ready" and data.get("role") == "sender":
                session_id = str(data.get("session_id") or "")
                paths = self._wormhole_pending.pop(session_id, [])
                try:
                    endpoint = str(data.get("local_endpoint") or "")
                    host, raw_port = endpoint.rsplit(":", 1)
                    peer_name = self.node.upsert_external_peer(
                        session_id, "一次性接收端", host.strip("[]"), int(raw_port),
                        "wormhole", str(data.get("endpoint_token") or ""))
                    self.node.select_peer(peer_name)
                    self._wormhole_active = session_id
                    for path in paths:
                        self._enqueue_path(path)
                except (ValueError, OSError) as exc:
                    self._route_status(f"短码通道建立失败：{exc}")
            elif name == "wormhole.error":
                session_id = str(data.get("session_id") or "")
                self._wormhole_pending.pop(session_id, None)
                if self._wormhole_active == session_id:
                    self.node.remove_external_peer(session_id, "wormhole")
                    self._wormhole_active = ""
                self._route_status(f"一次性短码失败：{data.get('error') or '连接已结束'}")
            elif name == "ssh.paired":
                peer = data.get("peer")
                if isinstance(peer, dict):
                    self._remember_ssh_peer(peer)
            elif name == "ssh.ready":
                self._ssh_session_id = str(data.get("session_id") or "")
                self._cross_network["ssh"]["remote_port"] = int(
                    data.get("remote_port") or 0)
                for peer in data.get("peers") or []:
                    if isinstance(peer, dict):
                        self._remember_ssh_peer(peer)
                _save_config(self._lan_cfg,
                             cross_network=copy.deepcopy(self._cross_network))
                self._route_status("SSH 中继已连接")
            elif name in {"ssh.config.error", "ssh.check.error", "ssh.pair.error"}:
                self._route_status(str(data.get("error") or "SSH 操作失败"))
            elif name == "ssh.disconnected":
                self._route_status("SSH 中继已断开，正在重连")
            elif name == "ssh.connected":
                self._route_status("SSH 中继已恢复")
            elif name == "ssh.data.error":
                peer_name = str(data.get("peer_name") or "对端")
                error = str(data.get("error") or "数据通道不可用")
                self._route_status(f"{peer_name} 的 SSH 传输通道异常：{error}")
            elif name == "core.error":
                self._route_status(str(data.get("error") or "跨网核心错误"))

        def _require_transport_core(self) -> TransportCore:
            if self._transport_core is None:
                raise TransportCoreError(self._transport_error or "跨网核心未运行")
            return self._transport_core

        @staticmethod
        def _summarize_paths(paths: list[str]) -> dict:
            return _summarize_transfer_paths(paths)

        def startOneTimeSend(self, paths: list[str]) -> None:
            def worker():
                workdir = ""
                try:
                    payload = self._summarize_paths(paths)
                    payload["summary"]["device_name"] = self._lan_cfg.peer_name
                    payload["summary"]["instance_id"] = self._lan_cfg.instance_id
                    result = self._require_transport_core().call(
                        "wormhole.create", {
                            "summary": payload["summary"],
                            "settings": self._wormhole_settings(),
                        }, timeout=30)
                    session_id = str(result.get("session_id") or "")
                    self._wormhole_pending[session_id] = payload["paths"]
                    self.transportEvent.emit("wormhole.code", result)
                except (TransportCoreError, OSError, ValueError) as exc:
                    self.transportEvent.emit("wormhole.error", {"error": str(exc)})
            threading.Thread(target=worker, daemon=True).start()

        def joinOneTime(self, code: str) -> None:
            def worker():
                try:
                    result = self._require_transport_core().call(
                        "wormhole.join", {
                            "code": str(code).strip(),
                            "settings": self._wormhole_settings(),
                        }, timeout=620)
                    self.transportEvent.emit("wormhole.offer", result)
                except TransportCoreError as exc:
                    self.transportEvent.emit("wormhole.error", {"error": str(exc)})
            threading.Thread(target=worker, daemon=True).start()

        def acceptOneTime(self, session_id: str) -> None:
            def worker():
                try:
                    self._require_transport_core().call(
                        "wormhole.accept", {"session_id": session_id}, timeout=60)
                except TransportCoreError as exc:
                    self.transportEvent.emit("wormhole.error", {
                        "session_id": session_id, "error": str(exc)})
            threading.Thread(target=worker, daemon=True).start()

        def rejectOneTime(self, session_id: str) -> None:
            core = self._transport_core
            if core is None:
                return
            threading.Thread(target=lambda: self._safe_core_call(
                "wormhole.reject", {"session_id": session_id}), daemon=True).start()

        def cancelTransportSession(self, session_id: str) -> None:
            core = self._transport_core
            if core is None or not session_id:
                return
            self._wormhole_pending.pop(session_id, None)
            if self._wormhole_active == session_id:
                self.node.remove_external_peer(session_id, "wormhole")
                self._wormhole_active = ""
            threading.Thread(target=lambda: self._safe_core_call(
                "session.cancel", {"session_id": session_id}), daemon=True).start()

        def _safe_core_call(self, method: str, params: dict) -> dict:
            try:
                return self._require_transport_core().call(method, params, timeout=30)
            except TransportCoreError:
                return {}

        def _wormhole_settings(self) -> dict:
            settings = self._cross_network["wormhole"]
            return {
                "rendezvous_url": settings.get("rendezvous_url", ""),
                "transit_relay": settings.get("transit_relay", ""),
                "timeout_minutes": 10,
            }

        # ---------- SSH VPS relay ----------
        def crossNetworkConfig(self) -> dict:
            result = copy.deepcopy(self._cross_network)
            profile = result["ssh"]["profile"]
            profile_id = profile["id"]
            profile["has_pasted_key"] = bool(secret_store.get(
                _ssh_secret_name(profile_id, "private_key")))
            profile["has_passphrase"] = bool(secret_store.get(
                _ssh_secret_name(profile_id, "passphrase")))
            result["core_available"] = self._transport_core is not None
            result["core_error"] = self._transport_error
            result["secure_store_available"] = secret_store.available()
            return result

        def _ssh_profile_payload(self, profile: dict,
                                 pasted_override: str | None = None,
                                 passphrase_override: str | None = None) -> dict:
            profile_id = str(profile.get("id") or "")
            mode = profile.get("private_key_mode")
            if mode == "paste":
                private_key = (pasted_override if pasted_override is not None else
                               secret_store.get(_ssh_secret_name(profile_id, "private_key")))
                if not private_key:
                    raise ValueError("请粘贴已有 SSH 私钥")
            else:
                path = os.path.expanduser(str(profile.get("private_key_path") or ""))
                if not os.path.isfile(path):
                    raise ValueError("请选择有效的 SSH 私钥文件")
                if os.path.getsize(path) > 1024 * 1024:
                    raise ValueError("SSH 私钥文件过大")
                with open(path, "r", encoding="utf-8") as handle:
                    private_key = handle.read()
            passphrase = (passphrase_override if passphrase_override is not None else
                          secret_store.get(_ssh_secret_name(profile_id, "passphrase")))
            return {
                "id": profile_id,
                "host": str(profile.get("host") or "").strip(),
                "port": int(profile.get("port") or 22),
                "user": str(profile.get("user") or "").strip(),
                "private_key": private_key,
                "private_key_label": str(profile.get("private_key_label") or ""),
                "passphrase": passphrase,
                "host_key_sha256": str(profile.get("host_key_sha256") or ""),
            }

        def checkSSHProfile(self, ssh_settings: dict, pasted_key: str | None,
                            passphrase: str | None) -> None:
            def worker():
                try:
                    normalized = _normalize_cross_network({"ssh": ssh_settings})["ssh"]
                    profile = self._ssh_profile_payload(
                        normalized["profile"], pasted_key, passphrase)
                    result = self._require_transport_core().call(
                        "ssh.check", {"profile": profile}, timeout=35)
                    self.transportEvent.emit("ssh.check.result", result)
                except (TransportCoreError, OSError, ValueError) as exc:
                    self.transportEvent.emit("ssh.check.error", {"error": str(exc)})
            threading.Thread(target=worker, daemon=True).start()

        def saveCrossNetworkConfig(self, wormhole_settings: dict,
                                   ssh_settings: dict,
                                   pasted_key: str | None = None,
                                   passphrase: str | None = None) -> bool:
            try:
                normalized = _normalize_cross_network({
                    "wormhole": wormhole_settings, "ssh": ssh_settings})
                profile = normalized["ssh"]["profile"]
                profile_id = profile["id"]
                if profile["private_key_mode"] == "paste" and pasted_key is not None:
                    if not secret_store.set(
                            _ssh_secret_name(profile_id, "private_key"), pasted_key):
                        raise ValueError("系统安全存储不可用，不能保存粘贴的私钥")
                    profile["private_key_label"] = "已存入系统安全存储"
                if passphrase is not None:
                    if passphrase and not secret_store.set(
                            _ssh_secret_name(profile_id, "passphrase"), passphrase):
                        raise ValueError("系统安全存储不可用，不能保存私钥口令")
                    if not passphrase:
                        secret_store.delete(_ssh_secret_name(profile_id, "passphrase"))
                if normalized["ssh"]["enabled"]:
                    self._ssh_profile_payload(profile)
                self._cross_network = normalized
                _save_config(self._lan_cfg, cross_network=copy.deepcopy(normalized))
                if normalized["ssh"]["enabled"]:
                    threading.Thread(target=self._restart_ssh_runtime, daemon=True).start()
                else:
                    self._stop_ssh_runtime()
                return True
            except (OSError, ValueError, TypeError) as exc:
                self.transportEvent.emit("ssh.config.error", {"error": str(exc)})
                return False

        def _restart_ssh_runtime(self) -> None:
            self._stop_ssh_runtime()
            self._start_ssh_runtime()

        def _stop_ssh_runtime(self) -> None:
            session_id = self._ssh_session_id
            self._ssh_session_id = ""
            if session_id:
                self._safe_core_call("session.cancel", {"session_id": session_id})
            for peer_id in list(self._ssh_runtime_peers):
                self.node.remove_external_peer(peer_id, "ssh")
            self._ssh_runtime_peers.clear()

        def _start_ssh_runtime(self) -> None:
            ssh_config = self._cross_network["ssh"]
            if not ssh_config.get("enabled"):
                return
            try:
                profile = self._ssh_profile_payload(ssh_config["profile"])
                profile_id = ssh_config["profile"]["id"]
                noise_read_ok, noise_private = secret_store.get_with_status(
                    _ssh_secret_name(profile_id, "noise_private"), timeout_seconds=30)
                if not noise_read_ok:
                    raise ValueError("系统安全存储暂时无法读取 SSH 身份，请稍后重试")
                if not noise_private and ssh_config.get("peers"):
                    raise ValueError("SSH 身份不可用，请删除旧设备并重新配对")
                result = self._require_transport_core().call("ssh.listen", {
                    "profile": profile,
                    "remote_port": int(ssh_config.get("remote_port") or 0),
                    "noise_private": noise_private,
                    "peers": ssh_config.get("peers") or [],
                }, timeout=45)
                generated = str(result.get("noise_private") or "")
                if generated and not secret_store.set(
                        _ssh_secret_name(profile_id, "noise_private"), generated):
                    self._safe_core_call("session.cancel", {
                        "session_id": str(result.get("session_id") or "")})
                    raise ValueError("系统安全存储不可用，无法保存 SSH 端到端身份")
                self.transportEvent.emit("ssh.ready", result)
            except (TransportCoreError, OSError, ValueError, TypeError) as exc:
                self.transportEvent.emit("ssh.config.error", {"error": str(exc)})

        def _remember_ssh_peer(self, peer: dict) -> None:
            try:
                peer_id = str(peer.get("id") or peer.get("instance_id") or "")
                runtime = dict(peer)
                if runtime.get("endpoint") and runtime.get("endpoint_token"):
                    self._ssh_runtime_peers[peer_id] = runtime
                    host, raw_port = str(runtime["endpoint"]).rsplit(":", 1)
                    self.node.upsert_external_peer(
                        peer_id, str(runtime.get("name") or "SSH 设备"),
                        host.strip("[]"), int(raw_port), "ssh",
                        str(runtime["endpoint_token"]),
                        str(runtime.get("instance_id") or ""))
                saved = {
                    "id": peer_id,
                    "name": str(peer.get("name") or "SSH 设备"),
                    "instance_id": str(peer.get("instance_id") or ""),
                    "remote_port": int(peer.get("remote_port") or 0),
                    "noise_public": str(peer.get("noise_public") or ""),
                    "end_to_end": bool(peer.get("end_to_end", True)),
                }
                peers = self._cross_network["ssh"]["peers"]
                peers[:] = [value for value in peers
                            if value.get("instance_id") != saved["instance_id"]]
                peers.append(saved)
                _save_config(self._lan_cfg,
                             cross_network=copy.deepcopy(self._cross_network))
            except (ValueError, TypeError, OSError) as exc:
                self._route_status(f"SSH 设备配置无效：{exc}")

        def _reinject_transport_peers(self) -> None:
            for peer_id, peer in list(self._ssh_runtime_peers.items()):
                try:
                    host, raw_port = str(peer["endpoint"]).rsplit(":", 1)
                    self.node.upsert_external_peer(
                        peer_id, str(peer.get("name") or "SSH 设备"),
                        host.strip("[]"), int(raw_port), "ssh",
                        str(peer.get("endpoint_token") or ""),
                        str(peer.get("instance_id") or ""))
                except (KeyError, ValueError, OSError):
                    continue

        def createSSHPairing(self) -> None:
            if not self._ssh_session_id:
                self.transportEvent.emit("ssh.pair.error", {"error": "SSH 中继尚未连接"})
                return
            def worker():
                try:
                    result = self._require_transport_core().call(
                        "ssh.pair.create", {"session_id": self._ssh_session_id}, timeout=15)
                    self.transportEvent.emit("ssh.pair.code", result)
                except TransportCoreError as exc:
                    self.transportEvent.emit("ssh.pair.error", {"error": str(exc)})
            threading.Thread(target=worker, daemon=True).start()

        def joinSSHPairing(self, code: str) -> None:
            if not self._ssh_session_id:
                self.transportEvent.emit("ssh.pair.error", {"error": "SSH 中继尚未连接"})
                return
            def worker():
                try:
                    result = self._require_transport_core().call(
                        "ssh.pair.join", {"session_id": self._ssh_session_id,
                                          "code": str(code).strip()}, timeout=45)
                    self.transportEvent.emit("ssh.pair.joined", result)
                except TransportCoreError as exc:
                    self.transportEvent.emit("ssh.pair.error", {"error": str(exc)})
            threading.Thread(target=worker, daemon=True).start()

        def removeSSHPeer(self, instance_id: str) -> None:
            peers = self._cross_network["ssh"]["peers"]
            peers[:] = [peer for peer in peers
                        if peer.get("instance_id") != instance_id]
            self.node.remove_external_peer(instance_id, "ssh")
            self._ssh_runtime_peers.pop(instance_id, None)
            _save_config(self._lan_cfg,
                         cross_network=copy.deepcopy(self._cross_network))
            if self._cross_network["ssh"]["enabled"]:
                threading.Thread(target=self._restart_ssh_runtime, daemon=True).start()

        def _on_send_busy_changed(self, busy: bool) -> None:
            self.sendStateChanged.emit(busy)
            self.transferStateChanged.emit(self._transfer_active())

        @Slot(result=bool)
        def isTransferActive(self) -> bool:
            return self._transfer_active()

        def _transfer_active(self) -> bool:
            with self._progress_lock:
                progress_active = self._progress_active
            return self._sendq.busy() or progress_active

        def lanConfig(self) -> P2PConfig:
            return self._lan_cfg

        def _on_received_file(self, path: str) -> None:
            self._recent.appendleft(path)
            self._save_recent()
            self.recentChanged.emit()
            self.emit_out.emit(os.path.basename(path))

        def _save_recent(self) -> None:
            _save_config(self._lan_cfg, recent_files=list(self._recent))

        def _on_progress(self, kind: str, name: str, done: int, total: int) -> None:
            key = (kind, name)
            now = time.monotonic()
            previous = self._speed_state.get(key)
            speed = 0.0
            if previous is not None:
                prev_time, prev_done, prev_speed = previous
                elapsed = now - prev_time
                if elapsed > 0 and done >= prev_done:
                    instant = (done - prev_done) / elapsed
                    speed = instant if prev_speed <= 0 else prev_speed * 0.65 + instant * 0.35
            self._speed_state[key] = (now, done, speed)
            with self._progress_lock:
                self._progress_generation += 1
                generation = self._progress_generation
                self._progress_active = True
                self._progress_key = key
            self.transferStateChanged.emit(True)

            def settle():
                with self._progress_lock:
                    if generation != self._progress_generation:
                        return
                    self._progress_active = False
                self.transferStateChanged.emit(self._transfer_active())

            threading.Timer(1.25, settle).start()
            pct = done * 100 // total if total else 100
            self.progress.emit(kind, pct)
            label = "↑ 发送" if kind == "send" else "↓ 接收"
            speed_text = ""
            if speed >= 1024 * 1024:
                speed_text = f" · {speed / 1024 / 1024:.1f} MB/s"
            elif speed >= 1024:
                speed_text = f" · {speed / 1024:.0f} KB/s"
            self._last_status = f"{label} {name} {pct}%{speed_text}"
            self.status.emit(self._last_status)

        def _on_transfer_end(self, kind: str, name: str, _completed: bool) -> None:
            key = (kind, name)
            self._speed_state.pop(key, None)
            with self._progress_lock:
                if self._progress_key != key:
                    return
                self._progress_generation += 1
                self._progress_active = False
                self._progress_key = None
            self.progressCleared.emit()
            self.transferStateChanged.emit(self._transfer_active())

        @Slot(result=bool)
        def cancelTransfer(self) -> bool:
            cancelled = self._sendq.cancel()
            if cancelled:
                self.status.emit("正在取消发送…")
            return cancelled

        def _apply_settings(self, peer_name: str | None = None,
                            secret: str | None = None,
                            port: int | None = None,
                            encryption_enabled: bool | None = None) -> None:
            """改名/改口令/加密开关/改端口:写回配置并重启 P2P 节点。

            节点重启会阻塞数秒,放到后台线程;_restart_gate 串行化多次保存。
            """
            def worker():
                self._restart_gate.acquire()
                self._restarting = True
                try:
                    self._apply_settings_blocking(
                        peer_name, secret, port, encryption_enabled)
                finally:
                    self._restarting = False
                    self._restart_gate.release()

            threading.Thread(target=worker, daemon=True).start()

        def _apply_settings_blocking(
                self, peer_name, secret, port, encryption_enabled) -> None:
            cfg = self._lan_cfg
            with self._engine_lock:
                if (secret is not None and secret != cfg.secret
                        and not secret_store.set(_TRANSFER_SECRET_NAME, secret)):
                    self._route_status(
                        "无法使用系统安全存储，传输口令未保存，设置未生效")
                    return
                selected_service = self.node._last_selected_service
                selected = self.node.selected_peer()
                if selected:
                    selected_service = next(
                        (peer.service_name for peer in self.node.peers()
                         if peer.name == selected), selected_service)
                self.node.stop()
                if peer_name is not None:
                    cfg.peer_name = peer_name
                if secret is not None:
                    cfg.secret = secret
                if encryption_enabled is not None:
                    cfg.encryption_enabled = bool(encryption_enabled)
                if port is not None:
                    cfg.listen_port = port
                _save_config(cfg)
                try:
                    self.node = self._make_node(cfg)
                except SystemExit:
                    # 设了口令但没装 cryptography：退回不加密，保持能用
                    cfg.encryption_enabled = False
                    _save_config(cfg)
                    self.node = self._make_node(cfg)
                    self.status.emit("缺少 cryptography 库，加密未开启")
                self.node._last_selected_service = selected_service
                self.node.start()
                self._retarget_transport_core()
                self._reinject_transport_peers()
            self.peersChanged.emit()

        def _route_status(self, msg: str) -> None:
            """出错信息走 persistentHint(持续显示)，普通信息走 hint(2.2s 消失)。"""
            print(f"[STATUS] {msg}", flush=True)
            self._last_status = msg
            if msg and ("失败" in msg or "无法" in msg or "未开启" in msg):
                self.errorState.emit(msg)
            else:
                self.errorState.emit("")  # 清除之前的错误
                self.status.emit(msg)

        @Slot()
        def refreshDiscovery(self) -> None:
            """手动重启设备发现(主界面刷新按钮)。mDNS 层偶发卡死的自救手段。"""
            node = self.node
            if isinstance(node, P2PNode):
                self.status.emit("正在重新搜索设备…")
                threading.Thread(
                    target=lambda: node._rebuild_mdns("手动刷新"),
                    daemon=True).start()

        @Slot(result="QVariantList")
        def localAddresses(self) -> list:
            """本机全部非环回 IPv4(含 Tailscale 100.x)。设置页展示用。"""
            try:
                return [ip for ip in self.node._get_local_ips()
                        if ip != "127.0.0.1"]
            except Exception:
                return []

        @Slot(result=int)
        def actualPort(self) -> int:
            """当前节点实际监听端口；未启动或固定端口不可用时返回 0。"""
            try:
                return int(self.node.actual_port)
            except Exception:
                return 0

        @Slot(result=str)
        def lastStatus(self) -> str:
            return self._last_status

        @Slot(result=str)
        def peerStatus(self) -> str:
            """持续状态：始终返回空(桌宠不显示持续文字，只有出错时才显示)。"""
            return ""

        @Slot(result=str)
        def missingTargetMessage(self) -> str:
            return "还没发现设备" if not self.node.peers() else "先点选一台目标设备"

        def _select_peer(self, name):
            """选中目标设备(由右键菜单触发)。"""
            self.node.select_peer(name)

        @Slot(str)
        def dropFile(self, url: str):
            """QML DropArea / 主窗口拖入的文件或目录，交给后台队列串行发送。"""
            if self._restarting:
                self.status.emit("正在应用新设置，请稍候再发送")
                return
            path = QUrl(url).toLocalFile() if url.startswith("file:") else url
            if not path:
                return
            if not self.node.selected_peer():
                self.status.emit(self.missingTargetMessage())
                return
            self._enqueue_path(path)

        def _enqueue_path(self, path: str):
            """文件或目录原样入队；扫描、流式发送和回退都在工作线程执行。"""
            if os.path.isfile(path) or os.path.isdir(path):
                self._sendq.put(path)

        def _on_batch_done(self, ok: int, total: int):
            """一批发送结束：多文件时聚合提示。

            目录与文件都由协议层在单个队列任务内完整处理。"""
            if total > 1:
                self.status.emit(f"已发送 {ok}/{total} 项")
            if self._wormhole_active:
                session_id = self._wormhole_active
                self._wormhole_active = ""
                self.node.remove_external_peer(session_id, "wormhole")
                threading.Thread(target=lambda: self._safe_core_call(
                    "session.cancel", {"session_id": session_id}), daemon=True).start()

        @Slot(result=bool)
        def hasTarget(self) -> bool:
            """QML 用来判断拖入文件时是否该播发送动画。"""
            return self.node.selected_peer() is not None

        @Slot(result=bool)
        def isTrustedOnly(self) -> bool:
            return self._lan_cfg.trusted_only

        @Slot(result=bool)
        def toggleTrustedOnly(self) -> bool:
            """切换「仅接收目标设备」：拦掉其他设备发来的文件。"""
            self._lan_cfg.trusted_only = not self._lan_cfg.trusted_only
            _save_config(self._lan_cfg)
            return self._lan_cfg.trusted_only

        @Slot(result="QVariantList")
        def recentFiles(self) -> list:
            """最近收到的文件路径列表(新的在前)。"""
            return [p for p in self._recent if os.path.exists(p)]

        @Slot()
        def clearRecent(self) -> None:
            """清空最近接收列表(只清记录,不动磁盘上的文件)。"""
            self._recent.clear()
            self._save_recent()
            self.recentChanged.emit()

        @Slot(str)
        def openPath(self, path: str):
            """用系统默认程序打开一个文件(最近接收菜单用)。"""
            try:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    import subprocess; subprocess.Popen(["open", path])
                else:
                    import subprocess; subprocess.Popen(["xdg-open", path])
            except Exception:
                self.status.emit("无法打开文件")

        @Slot(result=str)
        def inboxPath(self) -> str:
            return os.path.abspath(self._lan_cfg.inbox)

        @Slot()
        def openInbox(self):
            """在系统文件管理器中打开收件箱目录(跨平台)。"""
            path = os.path.abspath(self._lan_cfg.inbox)
            os.makedirs(path, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    import subprocess; subprocess.Popen(["open", path])
                else:
                    import subprocess; subprocess.Popen(["xdg-open", path])
            except Exception as e:
                self.status.emit(f"打开收件箱失败")
                print(f"[托盘] 打开收件箱失败: {e}", flush=True)

        @Slot()
        def chooseInbox(self):
            """打开目录选择对话框，让用户自定义收件箱路径。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QFileDialog
            directory = QFileDialog.getExistingDirectory(
                None, "选择收件箱目录", self._lan_cfg.inbox)
            if directory:
                self._lan_cfg.inbox = directory
                os.makedirs(directory, exist_ok=True)
                _save_config(self._lan_cfg)   # 记住，重启后仍生效
                self.status.emit(f"收件箱: {os.path.basename(directory)}")

        @Slot(str)
        def setInbox(self, directory: str) -> None:
            """设置收件箱路径并持久化(主界面「保存」用)。

            与 chooseInbox 的差别:直接收路径,无对话框。即使其他设置项没变,
            也保证收件箱改动落盘——修复"只改收件箱、重启后回退"的问题。
            """
            directory = (directory or "").strip()
            if not directory:
                return
            if os.path.abspath(directory) == os.path.abspath(self._lan_cfg.inbox):
                return   # 没变,不必写盘
            self._lan_cfg.inbox = directory
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
            _save_config(self._lan_cfg)

        def setInboxClassification(self, enabled: bool, directories: dict) -> None:
            """Persist automatic receive routing without rebuilding the node."""
            source = directories if isinstance(directories, dict) else {}
            normalized = {
                category: str(source.get(category) or "").strip()
                for category in ("media", "archive", "file", "folder")
            }
            enabled = bool(enabled)
            if (enabled == self._lan_cfg.inbox_auto_classify
                    and normalized == self._lan_cfg.inbox_category_dirs):
                return
            self._lan_cfg.inbox_auto_classify = enabled
            self._lan_cfg.inbox_category_dirs = normalized
            if enabled:
                for directory in normalized.values():
                    if directory:
                        try:
                            os.makedirs(os.path.expanduser(directory), exist_ok=True)
                        except OSError:
                            pass
            _save_config(self._lan_cfg)

        # ---- 手动设备(Tailscale/固定 IP 直连) ----
        @Slot(result="QVariantList")
        def manualPeers(self) -> list:
            return [dict(m) for m in (self._lan_cfg.manual_peers or [])]

        @Slot("QVariantList", result=bool)
        def setManualPeers(self, peers: list) -> bool:
            """一次提交设置页的手动设备草稿，并同步当前节点。"""
            old = [dict(entry) for entry in (self._lan_cfg.manual_peers or [])]
            old_by_key = {(str(entry["host"]), int(entry["port"])): entry
                          for entry in old}
            normalized: list[dict] = []
            positions: dict[tuple[str, int], int] = {}
            try:
                for raw in peers or []:
                    host = str(raw.get("host", "")).strip()
                    name = str(raw.get("name", "")).strip()
                    port = int(raw.get("port", 0))
                    if not host or " " in host or not 1 <= port <= 65535:
                        return False
                    entry = {"name": name, "host": host, "port": port}
                    instance_id = str(raw.get("instance_id") or "").lower()
                    key = (host, port)
                    previous_id = str(
                        old_by_key.get(key, {}).get("instance_id") or "").lower()
                    # Existing endpoint identities are immutable here. Trust can only
                    # be reset by deleting the entry and adding it again.
                    candidate_id = previous_id or instance_id
                    if (len(candidate_id) == 32
                            and all(ch in "0123456789abcdef" for ch in candidate_id)):
                        entry["instance_id"] = candidate_id
                    if key in positions:
                        normalized[positions[key]] = entry
                    else:
                        positions[key] = len(normalized)
                        normalized.append(entry)
            except (AttributeError, TypeError, ValueError):
                return False

            if old == normalized:
                return True
            new_by_key = {(entry["host"], entry["port"]): entry
                          for entry in normalized}
            if isinstance(self.node, P2PNode):
                for key, entry in old_by_key.items():
                    if key not in new_by_key:
                        self.node.remove_manual_peer(*key)
                for key, entry in new_by_key.items():
                    if key not in old_by_key or old_by_key[key] != entry:
                        self.node.add_manual_peer(
                            entry["name"], entry["host"], entry["port"])
            self._lan_cfg.manual_peers = [dict(entry) for entry in normalized]
            _save_config(self._lan_cfg)
            return True

        @Slot(str, str, int, result=bool)
        def addManualPeer(self, name: str, host: str, port: int) -> bool:
            """添加手动设备并持久化；验证成功后进入设备列表。"""
            host = (host or "").strip()
            name = (name or "").strip()
            try:
                port = int(port)
            except (TypeError, ValueError):
                return False
            if not host or " " in host or not 1 <= port <= 65535:
                return False
            if isinstance(self.node, P2PNode):
                self.node.add_manual_peer(name, host, port)   # 就地注册(共享 cfg)
            else:
                # 远程模式:只更新配置,切回局域网时 start() 会注册
                self._lan_cfg.manual_peers = [
                    m for m in (self._lan_cfg.manual_peers or [])
                    if not (m["host"] == host and int(m["port"]) == port)]
                self._lan_cfg.manual_peers.append(
                    {"name": name, "host": host, "port": port})
            _save_config(self._lan_cfg)
            return True

        @Slot(str, int)
        def removeManualPeer(self, host: str, port: int) -> None:
            if isinstance(self.node, P2PNode):
                self.node.remove_manual_peer(host, int(port))
            else:
                self._lan_cfg.manual_peers = [
                    m for m in (self._lan_cfg.manual_peers or [])
                    if not (m["host"] == host and int(m["port"]) == int(port))]
            _save_config(self._lan_cfg)

        @Slot()
        def renameDevice(self):
            """改本机显示名(对端右键菜单里看到的名字)。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(
                None, "设备名称", "对端设备看到的名字：",
                text=self._lan_cfg.peer_name)
            if ok:
                name = name.strip()
                if name and name != self._lan_cfg.peer_name:
                    self._apply_settings(peer_name=name)
                    self.status.emit(f"设备名: {name}")

        @Slot()
        def changeSecret(self):
            """设置/修改/清除端到端加密口令。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            secret, ok = QInputDialog.getText(
                None, "端到端加密口令",
                "两台设备口令必须一致；留空关闭加密：",
                QLineEdit.Normal, self._lan_cfg.secret)
            enabled = bool(secret)
            if ok and (secret != self._lan_cfg.secret
                       or enabled != self._lan_cfg.encryption_enabled):
                self._apply_settings(secret=secret,
                                     encryption_enabled=enabled)
                self.status.emit("已开启端到端加密" if secret else "已关闭加密")

        @Slot(result=bool)
        def isAutoStart(self) -> bool:
            """是否已设置开机自启。"""
            return is_autostart_enabled()

        @Slot(result=bool)
        def toggleAutoStart(self) -> bool:
            """切换开机自启，返回切换后的状态。"""
            enabled = not is_autostart_enabled()
            return set_autostart(enabled, self._lan_cfg)

        @Slot(result=str)
        def connState(self) -> str:
            """给菜单状态行用的当前状态文字(零网络开销)。"""
            peers = self.node.peers()
            if not peers:
                return "🔍 搜索设备中…"
            selected = self.node.selected_peer()
            if selected:
                return f"→ {selected}"
            return f"🔍 {len(peers)}台设备"

        @Slot()
        def showMain(self):
            """显示并激活主窗口(托盘单击/右键菜单"打开主界面")。"""
            w = getattr(self, "_main_window", None)
            if w is not None:
                w.show()
                w.raise_()
                w.activateWindow()

        @Slot()
        def hidePet(self):
            """关闭桌宠挂件(桌宠右键菜单"关闭桌宠")。

            等价于主界面设置里关掉「桌宠挂件」开关:只收起挂件并记住,
            程序与传输继续运行;重新开启走主界面设置。
            """
            hide = getattr(self, "_hide_pet", None)
            if hide is not None:
                hide()
                self.status.emit("桌宠已关闭，可在主界面设置中重新开启")

        # ---- 版本与更新 ----
        @Slot(result=str)
        def appVersion(self) -> str:
            return _APP_VERSION

        @Slot(result=str)
        def releasesPage(self) -> str:
            return _RELEASES_PAGE

        @Slot(result=str)
        def repositoryPage(self) -> str:
            return _REPOSITORY_PAGE

        @Slot()
        def checkUpdate(self) -> None:
            """查 GitHub 最新 Release 并与当前版本比较(后台线程)。

            走系统代理(urllib 自动读 Windows 注册表代理设置,Clash 系统代理
            开着就能通);超时/不可达按"检查失败"报告,引导手动去下载页。

            GitHub API 匿名限流每小时 60 次且按出口 IP 计(挂代理时出口
            IP 共享,极易 403),API 失败后回退解析 releases/latest 的
            重定向拿 tag——网页端无此限流,只是拿不到更新说明。
            """
            def worker():
                import urllib.request
                want = ("InkHolePet-windows.zip" if sys.platform == "win32"
                        else "InkHolePet-macos.zip")
                latest, notes, asset_url = "", "", ""
                try:
                    import json as _json
                    req = urllib.request.Request(
                        _RELEASES_API,
                        headers={"User-Agent": "InkHole-Updater",
                                 "Accept": "application/vnd.github+json"})
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        data = _json.loads(resp.read().decode("utf-8"))
                    latest = str(data.get("tag_name", "")).strip()
                    notes = _summarize_release_notes(str(data.get("body", "") or ""))
                    for asset in data.get("assets", []):
                        if asset.get("name") == want:
                            asset_url = str(asset.get("browser_download_url", ""))
                            break
                except Exception as api_exc:
                    try:
                        req = urllib.request.Request(
                            _RELEASES_PAGE,
                            headers={"User-Agent": "InkHole-Updater"})
                        with urllib.request.urlopen(req, timeout=12) as resp:
                            final_url = resp.geturl()
                        if "/tag/" in final_url:
                            latest = final_url.rstrip("/").rsplit("/tag/", 1)[-1]
                            asset_url = (
                                "https://github.com/RexVane/InkHole/releases"
                                f"/download/{latest}/{want}")
                    except Exception:
                        self.updateCheckFinished.emit(
                            False, "", f"检查失败：{api_exc}", "")
                        return
                if not latest:
                    self.updateCheckFinished.emit(
                        False, "", "检查失败：无法解析最新版本号", "")
                    return
                has_new = _version_newer(latest, _APP_VERSION)
                self.updateCheckFinished.emit(has_new, latest, notes, asset_url)

            threading.Thread(target=worker, daemon=True).start()

        @Slot(str)
        def performUpdate(self, asset_url: str) -> None:
            """Windows 打包版自动更新:下载 zip → 写替换脚本 → 退出交棒。

            运行中的 exe 无法覆盖自己,由 bat 在进程退出后解压覆盖并重启。
            仅 frozen(打包)且 Windows 下可用;其他环境走浏览器下载页。
            """
            if not (getattr(sys, "frozen", False) and sys.platform == "win32"
                    and asset_url):
                self.openPath(_RELEASES_PAGE)
                return

            def worker():
                workdir = ""
                try:
                    import tempfile
                    import urllib.request
                    self.status.emit("正在下载新版本…")
                    workdir = tempfile.mkdtemp(prefix="inkhole_update_")
                    zip_path = os.path.join(workdir, "InkHolePet-windows.zip")
                    req = urllib.request.Request(
                        asset_url, headers={"User-Agent": "InkHole-Updater"})
                    with urllib.request.urlopen(req, timeout=30) as resp, \
                            open(zip_path, "wb") as out:
                        total = int(resp.headers.get("Content-Length") or 0)
                        if total > 512 * 1024 * 1024:
                            raise ValueError("更新包下载大小异常")
                        done = 0
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            if done > 512 * 1024 * 1024:
                                raise ValueError("更新包下载大小异常")
                            if total:
                                self.status.emit(
                                    f"正在下载新版本… {done * 100 // total}%")
                    manifest_path = zip_path + ".sha256.ps1"
                    checksum_request = urllib.request.Request(
                        asset_url + ".sha256.ps1",
                        headers={"User-Agent": "InkHole-Updater"})
                    with urllib.request.urlopen(checksum_request, timeout=15) as resp:
                        checksum_manifest = resp.read(128 * 1024 + 1)
                    if len(checksum_manifest) > 128 * 1024:
                        raise ValueError("更新校验文件过大")
                    with open(manifest_path, "wb") as manifest_output:
                        manifest_output.write(checksum_manifest)

                    self.status.emit("正在验证发布签名…")
                    unzip_dir = os.path.join(workdir, "unzip")
                    _extract_update_zip(zip_path, unzip_dir)
                    candidate_exe = os.path.join(
                        unzip_dir, "InkHolePet", "InkHolePet.exe")
                    if not os.path.isfile(candidate_exe):
                        raise ValueError("更新包缺少 InkHolePet.exe")
                    _verify_windows_update_signature(
                        sys.executable, candidate_exe, manifest_path, workdir)
                    expected_hash = _parse_release_checksum(
                        checksum_manifest, os.path.basename(zip_path))
                    actual_hash = _sha256_path(zip_path)
                    if not hmac.compare_digest(actual_hash, expected_hash):
                        raise ValueError("更新包 SHA-256 校验失败")
                    app_dir = os.path.dirname(sys.executable)
                    bat = os.path.join(workdir, "update.bat")
                    backup_dir = os.path.join(workdir, "backup")
                    with open(bat, "w", encoding="gbk", errors="ignore") as f:
                        f.write(_windows_update_script(
                            app_dir, backup_dir, unzip_dir, zip_path,
                            manifest_path))
                    self.status.emit("下载完成，正在重启应用…")
                    import subprocess
                    subprocess.Popen(
                        ["cmd", "/c", bat],
                        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                        close_fds=True)
                    self.quit()   # quit 内部会停节点并退出事件循环(线程安全)
                except Exception as exc:
                    if workdir:
                        shutil.rmtree(workdir, ignore_errors=True)
                    self.status.emit(f"自动更新失败：{exc}，请到下载页手动更新")

            threading.Thread(target=worker, daemon=True).start()

        @Slot()
        def showMenu(self):
            """桌宠被右键时,在鼠标位置弹出菜单。"""
            if self._tray_menu is not None:
                from PySide6.QtGui import QCursor
                self._tray_menu.popup(QCursor.pos())
            else:
                self.showMain()

        @Slot()
        def quit(self):
            """退出:节点停止放后台并限时等待,不让 mDNS 注销卡住退出。"""
            if self._transport_core is not None:
                self._transport_core.close()
                self._transport_core = None
            closer = threading.Thread(target=self.node.stop, daemon=True)
            closer.start()
            closer.join(2.0)   # 给 mDNS goodbye 留 2 秒,超时直接退
            QGuiApplication.quit()

    # 有 QtWidgets 用 QApplication(支持托盘菜单),否则退回 QGuiApplication
    app.setApplicationName("墨洞")
    app_icon_scale = MACOS_ICON_SCALE if sys.platform == "darwin" else 1.0
    app.setWindowIcon(_make_app_icon(app_icon_scale))
    app.setQuitOnLastWindowClosed(False)         # 关挂件窗口不退出(托盘还在),仅菜单"退出"才退
    bridge = Bridge(cfg)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("petSizePx", _adaptive_pet_size(size_override))
    engine.load(QUrl.fromLocalFile(_QML_FILE))
    if not engine.rootObjects():
        sys.stderr.write("QML 加载失败\n")
        raise SystemExit(1)

    # 桌宠 = 主界面里可开关的选项(show_pet)，默认开启
    pet_root = engine.rootObjects()[0]
    show_pet = bool(_load_saved_config().get("show_pet", True))
    pet_root.setVisible(show_pet)

    def _set_pet_visible(on: bool) -> None:
        pet_root.setVisible(bool(on))
        _save_config(bridge._lan_cfg, show_pet=bool(on))

    # 桌宠右键"关闭桌宠"走这里:与设置页开关同一持久化路径
    bridge._hide_pet = lambda: _set_pet_visible(False)

    def _apply_identity(name: str, secret: str, port: int,
                        encryption_enabled: bool) -> None:
        c = bridge._lan_cfg
        if (name, secret, port, encryption_enabled) == (
                c.peer_name, c.secret, c.listen_port, c.encryption_enabled):
            return   # 没变就不重启节点
        bridge._apply_settings(peer_name=name, secret=secret, port=port,
                               encryption_enabled=encryption_enabled)

    # 主界面窗口(需要 QtWidgets；不可用时退回纯桌宠模式)
    main_win = None
    if _HAS_WIDGETS:
        from .mainwindow import MainWindow
        main_win = MainWindow(bridge, {
            "pet_visible": lambda: pet_root.isVisible(),
            "set_pet_visible": _set_pet_visible,
            "is_autostart": is_autostart_enabled,
            "set_autostart": lambda on: set_autostart(on, bridge._lan_cfg),
            "apply_settings": _apply_identity,
        }, icon=_make_app_icon())
        bridge._main_window = main_win
        main_win.show()
        # 首次启动自动弹一次使用说明,看过即记住(与 show_pet 同一持久化路径)
        if not _load_saved_config().get("usage_guide_seen", False):
            main_win._show_usage_guide()
            _save_config(bridge._lan_cfg, usage_guide_seen=True)

    if _TRANSFER_SECRET_WARNING:
        bridge._route_status(_TRANSFER_SECRET_WARNING)

    # 系统托盘(Windows 右下托盘 / macOS 顶部菜单栏,Qt 跨平台一套代码)
    _tray = _setup_tray(app, bridge)   # 返回托盘对象(需持引用防被回收),失败返回 None
    # macOS: 让挂件常驻所有桌面(Spaces)，切换桌面/应用失焦都不消失
    if sys.platform == "darwin":
        try:
            from AppKit import NSApp
            # canJoinAllSpaces(1<<0) | stationary(1<<4) | fullScreenAuxiliary(1<<8)
            # 注意先清掉 Qt.Tool 自带的 moveToActiveSpace(1<<1)，两者互斥
            for w in NSApp.windows():
                behavior = (w.collectionBehavior() & ~(1 << 1)) | (1 << 0) | (1 << 4) | (1 << 8)
                w.setCollectionBehavior_(behavior)
                w.setHidesOnDeactivate_(False)
        except ImportError:
            pass  # 未装 pyobjc 时跳过：功能不受影响，只是切桌面时挂件会隐藏
    os.makedirs(cfg.inbox, exist_ok=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
