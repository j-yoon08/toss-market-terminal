from __future__ import annotations

import json
from pathlib import Path

import pytest

from toss_market_terminal.config import CredentialError, Credentials


def write_credentials(path: Path, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(
            {
                "client_id": "tsck_live_test_identifier",
                "client_secret": "tssk_live_test_secret_value_long_enough",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)


def test_load_secure_credentials(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    write_credentials(path)
    credentials = Credentials.load(path)
    assert credentials.client_id.startswith("tsck_live_")
    assert credentials.client_secret.startswith("tssk_live_")


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
