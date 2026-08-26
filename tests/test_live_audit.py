from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from toss_market_terminal.live_audit import LiveAuditLog, LiveAuditLogError
from toss_market_terminal.live_order import LiveAuditRecord

FINGERPRINT = "a" * 64
CLIENT_ORDER_ID = "tmt-" + "b" * 32


def record(**overrides: object) -> LiveAuditRecord:
    values: dict[str, object] = {
        "fingerprint": FINGERPRINT,
        "client_order_id": CLIENT_ORDER_ID,
        "side": "BUY",
        "symbol": "AAPL",
        "quantity": Decimal("1.2500"),
        "attempted_at": datetime(2026, 8, 26, 12, 34, 56, 123456, tzinfo=UTC),
        "status": "accepted",
        "safe_code": "SUBMIT_ACCEPTED",
    }
    values.update(overrides)
    return LiveAuditRecord(**values)  # type: ignore[arg-type]


def read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_first_and_second_append_are_exact_private_json_lines(tmp_path: Path) -> None:
    leaf = tmp_path / "state"
    path = leaf / "audit.jsonl"
    log = LiveAuditLog(path)

    log.append(record())
    log.append(record(fingerprint="c" * 64, client_order_id="tmt-" + "d" * 32, side="SELL"))

    rows = read_rows(path)
    assert len(rows) == 2
    assert rows[0] == {
        "schemaVersion": "1",
        "fingerprint": FINGERPRINT,
        "clientOrderId": CLIENT_ORDER_ID,
        "side": "BUY",
        "symbol": "AAPL",
        "quantity": "1.25",
        "attemptedAt": "2026-08-26T12:34:56.123456Z",
        "status": "accepted",
        "safeCode": "SUBMIT_ACCEPTED",
    }
    assert rows[1]["side"] == "SELL"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_prepare_creates_empty_private_target_without_audit_event(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    LiveAuditLog(path).prepare()
    assert path.read_bytes() == b""
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_0755_ancestor_is_preserved(tmp_path: Path) -> None:
    ancestor = tmp_path / "existing"
    ancestor.mkdir(mode=0o755)
    os.chmod(ancestor, 0o755)
    path = ancestor / "private-leaf" / "audit.jsonl"

    LiveAuditLog(path).append(record())

    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_concurrent_appends_keep_one_valid_object_per_line(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    log = LiveAuditLog(path)

    def append_one(index: int) -> None:
        log.append(
            record(
                fingerprint=f"{index:064x}",
                client_order_id="tmt-" + f"{index:032x}",
                side="BUY" if index % 2 == 0 else "SELL",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_one, range(32)))

    rows = read_rows(path)
    assert len(rows) == 32
    assert len({row["fingerprint"] for row in rows}) == 32


def test_final_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    path = leaf / "audit.jsonl"
    path.symlink_to(target)

    with pytest.raises(LiveAuditLogError) as caught:
        LiveAuditLog(path).append(record())

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert str(path) not in str(caught.value)


def test_symlink_parent_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(LiveAuditLogError):
        LiveAuditLog(linked / "audit.jsonl").append(record())
    assert list(real.iterdir()) == []


@pytest.mark.parametrize(
    "changed",
    [
        {"fingerprint": "x" * 64},
        {"client_order_id": "bad"},
        {"side": "HOLD"},
        {"symbol": "bad symbol"},
        {"quantity": Decimal("0")},
        {"quantity": Decimal("NaN")},
        {"quantity": "1"},
        {"attempted_at": datetime(2026, 8, 26)},
        {"attempted_at": "2026-08-26T00:00:00Z"},
        {"status": "filled"},
        {"safe_code": "Bearer token"},
        {"safe_code": "X\nY"},
    ],
)
def test_malformed_record_is_rejected_before_file_creation(
    tmp_path: Path, changed: dict[str, object]
) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    with pytest.raises(LiveAuditLogError) as caught:
        LiveAuditLog(path).append(replace(record(), **changed))
    assert not path.exists()
    assert str(path) not in str(caught.value)
    assert "Bearer" not in str(caught.value)


def test_existing_file_wrong_mode_is_rejected(tmp_path: Path) -> None:
    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    path = leaf / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)

    with pytest.raises(LiveAuditLogError):
        LiveAuditLog(path).append(record())
    assert path.read_text(encoding="utf-8") == ""


def test_repr_redacts_path(tmp_path: Path) -> None:
    path = tmp_path / "secret-name" / "audit.jsonl"
    rendered = repr(LiveAuditLog(path))
    assert str(path) not in rendered
    assert "<redacted>" in rendered
