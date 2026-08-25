from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from toss_market_terminal.settings import (
    DEFAULT_SETTINGS_PATH,
    MAX_ALERTS,
    MAX_WATCHLIST_ENTRIES,
    SETTINGS_VERSION,
    AlertRule,
    Settings,
    SettingsError,
    SettingsStore,
    next_alert_id,
    parse_threshold,
    with_alert,
    with_watchlist_symbol,
    without_alert,
    without_watchlist_symbol,
)


def test_default_settings_path_is_bounded_config_location() -> None:
    assert DEFAULT_SETTINGS_PATH == Path.home() / ".config" / "toss-market" / "settings.json"


def test_missing_file_yields_empty_settings(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "nested" / "settings.json").load()
    assert settings == Settings()
    assert settings.version == SETTINGS_VERSION
    assert settings.watchlist == ()
    assert settings.alerts == ()


def test_round_trip_preserves_order_and_decimal_strings(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    settings = Settings()
    for raw in ("005930", "AAPL", "nvda"):
        settings, _created = with_watchlist_symbol(settings, raw)
    settings, first = with_alert(settings, "AAPL", "above", "250")
    settings, second = with_alert(settings, "005930", "volume-spike", "3.5")
    assert (first.id, second.id) == ("A1", "A2")

    store.save(settings)

    raw_payload = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert raw_payload["version"] == SETTINGS_VERSION
    assert raw_payload["watchlist"] == ["005930", "AAPL", "NVDA"]
    assert raw_payload["alerts"][0] == {
        "id": "A1",
        "symbol": "AAPL",
        "kind": "above",
        "threshold": "250",
        "enabled": True,
    }

    loaded = store.load()
    assert loaded == settings
    assert loaded.alerts[1].threshold == Decimal("3.5")


def test_watchlist_duplicate_reports_exists_and_keeps_order() -> None:
    settings = Settings(watchlist=("AAPL", "NVDA"))
    updated, created = with_watchlist_symbol(settings, "aapl")
    assert created is False
    assert updated == settings
    updated, created = with_watchlist_symbol(settings, "005930")
    assert created is True
    assert updated.watchlist == ("AAPL", "NVDA", "005930")


def test_watchlist_rejects_more_than_max_entries() -> None:
    settings = Settings()
    for index in range(MAX_WATCHLIST_ENTRIES):
        settings, created = with_watchlist_symbol(settings, f"S{index}")
        assert created
    with pytest.raises(SettingsError):
        with_watchlist_symbol(settings, "OVERFLOW")
    with pytest.raises(ValueError):
        with_watchlist_symbol(settings, "bad symbol!")


def test_watchlist_remove_absent_entry_raises() -> None:
    settings = Settings(watchlist=("AAPL",))
    with pytest.raises(SettingsError):
        without_watchlist_symbol(settings, "NVDA")
    assert without_watchlist_symbol(settings, "aapl").watchlist == ()


def test_load_rejects_duplicate_or_unnormalized_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"version": 1, "watchlist": ["AAPL", "aapl"], "alerts": []}),
        encoding="utf-8",
    )
    with pytest.raises(SettingsError):
        SettingsStore(path).load()

    path.write_text(
        json.dumps({"version": 1, "watchlist": ["bad symbol!"], "alerts": []}),
        encoding="utf-8",
    )
    with pytest.raises(SettingsError):
        SettingsStore(path).load()


def test_load_rejects_unknown_version_kind_and_bounds(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")

    def write(payload: object) -> None:
        (tmp_path / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    write({"version": 2, "watchlist": [], "alerts": []})
    with pytest.raises(SettingsError):
        store.load()

    write(
        {
            "version": 1,
            "watchlist": [],
            "alerts": [
                {
                    "id": "A1",
                    "symbol": "AAPL",
                    "kind": "rocket",
                    "threshold": "1",
                    "enabled": True,
                }
            ],
        }
    )
    with pytest.raises(SettingsError):
        store.load()

    write(
        {
            "version": 1,
            "watchlist": [f"S{i}" for i in range(MAX_WATCHLIST_ENTRIES + 1)],
            "alerts": [],
        }
    )
    with pytest.raises(SettingsError):
        store.load()

    overflowing = [
        {"id": f"A{i}", "symbol": "AAPL", "kind": "above", "threshold": "1", "enabled": True}
        for i in range(MAX_ALERTS + 1)
    ]
    write({"version": 1, "watchlist": [], "alerts": overflowing})
    with pytest.raises(SettingsError):
        store.load()

    write(
        {
            "version": 1,
            "watchlist": [],
            "alerts": [
                {"id": "A1", "symbol": "AAPL", "kind": "above", "threshold": "-2", "enabled": True}
            ],
        }
    )
    with pytest.raises(SettingsError):
        store.load()


def test_threshold_must_be_finite_positive_decimal_string() -> None:
    assert parse_threshold("250") == Decimal("250")
    assert parse_threshold("2.5") == Decimal("2.5")
    assert parse_threshold(7) == Decimal("7")
    for bad in ("0", "-1", "NaN", "Infinity", "-Infinity", "abc", "", None, 3.5, True, ["1"]):
        with pytest.raises(SettingsError):
            parse_threshold(bad)


def test_alert_ids_allocate_monotonically_from_existing_file(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    settings = store.load()
    settings, first = with_alert(settings, "AAPL", "above", "1")
    settings, second = with_alert(settings, "AAPL", "below", "2")
    assert (first.id, second.id) == ("A1", "A2")
    store.save(settings)

    reloaded = store.load()
    third = with_alert(reloaded, "NVDA", "change-above", "3")[1]
    assert third.id == "A3"

    gappy = replace(reloaded, alerts=(replace(second, id="A5"),))
    assert next_alert_id(gappy) == "A6"


def test_alert_remove_absent_id_raises() -> None:
    settings = Settings(alerts=(AlertRule("A1", "AAPL", "above", Decimal("1")),))
    with pytest.raises(SettingsError):
        without_alert(settings, "A9")
    assert without_alert(settings, "A1").alerts == ()


def test_save_rejects_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    link = tmp_path / "settings.json"
    real.write_text("{}", encoding="utf-8")
    link.symlink_to(real)
    store = SettingsStore(link)
    with pytest.raises(SettingsError):
        store.load()
    with pytest.raises(SettingsError):
        store.save(Settings())
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "{}"


def test_atomic_save_sets_permissions_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "settings.json"
    store = SettingsStore(target)
    store.save(Settings(watchlist=("AAPL",)))

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(target.parent).st_mode) == 0o700
    leftovers = [item.name for item in target.parent.iterdir() if item.name != "settings.json"]
    assert leftovers == []

    store.save(Settings())
    leftovers = [item.name for item in target.parent.iterdir()]
    assert leftovers == ["settings.json"]
    assert store.load() == Settings()


def test_save_does_not_chmod_an_existing_parent_directory(tmp_path: Path) -> None:
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    os.chmod(shared_parent, 0o755)
    target = shared_parent / "settings.json"

    SettingsStore(target).save(Settings(watchlist=("AAPL",)))

    assert stat.S_IMODE(os.stat(shared_parent).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_error_messages_do_not_echo_raw_file_content(tmp_path: Path) -> None:
    marker = "RAWCONTENT-SENTINEL-7351"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"note": marker}), encoding="utf-8")
    with pytest.raises(SettingsError) as caught:
        SettingsStore(path).load()
    assert marker not in str(caught.value)

    path.write_text("{not-json " + marker, encoding="utf-8")
    with pytest.raises(SettingsError) as caught:
        SettingsStore(path).load()
    assert marker not in str(caught.value)
