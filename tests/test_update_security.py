import hashlib
import stat
import zipfile

import pytest

from inkhole.pet import (_extract_update_zip, _parse_release_checksum,
                         _sha256_path, _windows_update_script)


def test_release_checksum_requires_expected_asset_name(tmp_path):
    archive = tmp_path / "InkHolePet-windows.zip"
    archive.write_bytes(b"signed release bytes")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    parsed = _parse_release_checksum(
        f"{digest}  {archive.name}\n".encode("ascii"), archive.name)
    assert parsed == digest
    assert _sha256_path(str(archive)) == digest

    signed_manifest = (
        f"# INKHOLE-SHA256 {digest}  {archive.name}\r\n"
        "# SIG # Begin signature block\r\n").encode("ascii")
    assert _parse_release_checksum(signed_manifest, archive.name) == digest

    with pytest.raises(ValueError):
        _parse_release_checksum(
            f"{digest}  other.zip\n".encode("ascii"), archive.name)


def test_update_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.exe", b"not allowed")

    with pytest.raises(ValueError, match="越界路径"):
        _extract_update_zip(str(archive), str(tmp_path / "extract"))
    assert not (tmp_path / "outside.exe").exists()


def test_update_zip_rejects_symbolic_links(tmp_path):
    archive = tmp_path / "update.zip"
    link = zipfile.ZipInfo("InkHolePet/link.dll")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(link, "../../outside.dll")

    with pytest.raises(ValueError, match="符号链接"):
        _extract_update_zip(str(archive), str(tmp_path / "extract"))


def test_windows_update_keeps_backup_when_rollback_fails():
    script = _windows_update_script(
        r"C:\InkHole", r"C:\Temp\backup", r"C:\Temp\unzip",
        r"C:\Temp\update.zip", r"C:\Temp\update.zip.sha256.ps1")

    rollback = script.index(":rollback")
    rollback_failed = script.index(":rollback_failed")
    cleanup = script.index(":cleanup")
    assert "if errorlevel 8 goto rollback_failed" in script[rollback:]
    assert cleanup < rollback_failed
    assert 'rd /s /q "C:\\Temp\\backup"' not in script[rollback_failed:]
