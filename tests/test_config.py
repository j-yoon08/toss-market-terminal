from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from toss_market_terminal import config
from toss_market_terminal.config import (
    CredentialError,
    Credentials,
    CredentialStore,
    mask_client_id,
    resolve_credentials_path,
)

CLIENT_ID = "tsck_live_test_identifier"
CLIENT_SECRET = "tssk_live_test_secret_value_long_enough"


def write_credentials(path: Path, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}),
        encoding="utf-8",
    )
    path.chmod(mode)


def credentials() -> Credentials:
    return Credentials.from_values(CLIENT_ID, CLIENT_SECRET)


def test_load_secure_credentials(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    write_credentials(path)
    loaded = Credentials.load(path)
    assert loaded.client_id == CLIENT_ID
    assert loaded.client_secret == CLIENT_SECRET


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [
        ("invalid", CLIENT_SECRET),
        (CLIENT_ID, "invalid"),
        (CLIENT_ID + "\n", CLIENT_SECRET),
        (CLIENT_ID, CLIENT_SECRET + " "),
        ("tsck_live_", CLIENT_SECRET),
        (CLIENT_ID, "tssk_live_"),
    ],
)
def test_from_values_rejects_malformed_credentials(client_id: str, client_secret: str) -> None:
    with pytest.raises(CredentialError):
        Credentials.from_values(client_id, client_secret)


def test_load_rejects_unknown_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    path.write_text(
        json.dumps(
            {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "access_token": "forbidden"}
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(CredentialError, match="알 수 없는"):
        Credentials.load(path)


def test_rejects_permissive_mode(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    write_credentials(path, 0o640)
    with pytest.raises(CredentialError, match="0600"):
        Credentials.load(path)


def test_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    link = tmp_path / "openapi.json"
    write_credentials(real)
    link.symlink_to(real)
    with pytest.raises(CredentialError, match="심볼릭"):
        Credentials.load(link)


def test_rejects_hardlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    link = tmp_path / "openapi.json"
    write_credentials(real)
    os.link(real, link)
    with pytest.raises(CredentialError, match="하드링크"):
        Credentials.load(link)


def test_wraps_stat_oserror_without_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "openapi.json"
    original_lstat = Path.lstat

    def guarded_lstat(candidate: Path, *args: object, **kwargs: object):
        if candidate == path:
            raise PermissionError(CLIENT_SECRET)
        return original_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)
    with pytest.raises(CredentialError) as caught:
        Credentials.load(path)
    assert CLIENT_SECRET not in str(caught.value)


def test_store_creates_private_parent_and_file(tmp_path: Path) -> None:
    path = tmp_path / "new" / "nested" / "credentials.json"
    store = CredentialStore(path)
    assert store.exists() is False
    store.save(credentials())
    assert store.exists() is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert Credentials.load(path) == credentials()


def test_store_preserves_existing_parent_mode(tmp_path: Path) -> None:
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "credentials.json"
    CredentialStore(path).save(credentials())
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_refuses_overwrite_without_replace(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    write_credentials(path)
    with pytest.raises(CredentialError, match="이미"):
        CredentialStore(path).save(credentials())
    assert Credentials.load(path) == credentials()


def test_store_replaces_atomically_without_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    first = credentials()
    second = Credentials.from_values("tsck_live_replacement_id", "tssk_live_replacement_secret")
    CredentialStore(path).save(first)
    CredentialStore(path).save(second, replace=True)
    assert Credentials.load(path) == second
    assert list(tmp_path.glob(".credentials.json.tmp-*")) == []


def test_store_replace_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    original = credentials()
    replacement = Credentials.from_values(
        "tsck_live_replacement_id", "tssk_live_replacement_secret"
    )
    CredentialStore(path).save(original)

    def fail_replace(_source: object, _destination: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(CredentialError, match="안전하게 저장"):
        CredentialStore(path).save(replacement, replace=True)
    assert Credentials.load(path) == original
    assert list(tmp_path.glob(".credentials.json.tmp-*")) == []


def test_store_rejects_final_symlink_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "credentials.json"
    link.symlink_to(target)
    with pytest.raises(CredentialError, match="심볼릭"):
        CredentialStore(link).save(credentials(), replace=True)
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_store_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    path = linked_parent / "credentials.json"
    with pytest.raises(CredentialError, match="심볼릭"):
        CredentialStore(path).save(credentials())
    assert not (real_parent / "credentials.json").exists()


def test_store_remove_rejects_symlink_and_removes_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    CredentialStore(path).save(credentials())
    CredentialStore(path).remove()
    assert not path.exists()

    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(CredentialError, match="심볼릭"):
        CredentialStore(path).remove()
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_resolve_credentials_path_prefers_new_then_legacy(tmp_path: Path) -> None:
    new = tmp_path / "new.json"
    legacy = tmp_path / "legacy.json"
    assert resolve_credentials_path(default_path=new, legacy_path=legacy) == new
    write_credentials(legacy)
    assert resolve_credentials_path(default_path=new, legacy_path=legacy) == legacy
    write_credentials(new)
    assert resolve_credentials_path(default_path=new, legacy_path=legacy) == new


def test_resolve_credentials_path_honors_explicit_path(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.json"
    assert resolve_credentials_path(explicit, default_path=tmp_path / "new") == explicit


def test_mask_client_id_never_reveals_full_value() -> None:
    masked = mask_client_id(CLIENT_ID)
    assert masked.endswith(CLIENT_ID[-4:])
    assert CLIENT_ID not in masked
    assert "•" in masked
