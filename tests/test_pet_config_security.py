import json

from inkhole import pet
from inkhole.p2p import P2PConfig


def _write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _fake_secure_store(monkeypatch, values, writable=True):
    monkeypatch.setattr(pet.secret_store, "get", lambda name: values.get(name, ""))
    monkeypatch.setattr(
        pet.secret_store, "get_with_status",
        lambda name: (True, values.get(name, "")),
    )

    def save(name, value):
        if not writable:
            return False
        if value:
            values[name] = value
        else:
            values.pop(name, None)
        return True

    monkeypatch.setattr(pet.secret_store, "set", save)


def test_build_config_migrates_plaintext_transfer_secret(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    _write_config(path, {
        "instance_id": "1" * 32,
        "name": "Mac",
        "secret": "legacy-password",
        "encryption_enabled": True,
    })
    monkeypatch.setattr(pet, "_config_path", lambda: str(path))
    secure = {}
    _fake_secure_store(monkeypatch, secure)

    cfg, _ = pet._build_config([])

    assert cfg.secret == "legacy-password"
    assert cfg.encryption_enabled
    assert secure[pet._TRANSFER_SECRET_NAME] == "legacy-password"
    assert "secret" not in json.loads(path.read_text(encoding="utf-8"))


def test_secure_transfer_secret_wins_and_legacy_field_is_scrubbed(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    _write_config(path, {
        "instance_id": "2" * 32,
        "secret": "stale-password",
        "encryption_enabled": True,
    })
    monkeypatch.setattr(pet, "_config_path", lambda: str(path))
    secure = {pet._TRANSFER_SECRET_NAME: "secure-password"}
    _fake_secure_store(monkeypatch, secure)

    cfg, _ = pet._build_config([])

    assert cfg.secret == "secure-password"
    assert "secret" not in json.loads(path.read_text(encoding="utf-8"))


def test_failed_migration_keeps_runtime_value_but_removes_plaintext(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    _write_config(path, {
        "instance_id": "3" * 32,
        "secret": "one-run-password",
        "encryption_enabled": True,
    })
    monkeypatch.setattr(pet, "_config_path", lambda: str(path))
    _fake_secure_store(monkeypatch, {}, writable=False)
    monkeypatch.setattr(pet.secret_store, "get_with_status", lambda _name: (False, ""))

    cfg, _ = pet._build_config([])

    assert cfg.secret == "one-run-password"
    assert cfg.encryption_enabled
    assert "仅在本次运行中生效" in pet._TRANSFER_SECRET_WARNING
    assert "secret" not in json.loads(path.read_text(encoding="utf-8"))


def test_save_config_cannot_reintroduce_plaintext_secret(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    _write_config(path, {"secret": "old"})
    monkeypatch.setattr(pet, "_config_path", lambda: str(path))
    cfg = P2PConfig(inbox=str(tmp_path), peer_name="Desktop", secret="runtime")

    pet._save_config(cfg, secret="extra-value", show_pet=True)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "secret" not in saved
    assert saved["show_pet"] is True
