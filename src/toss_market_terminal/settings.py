"""Versioned, non-secret user preferences: watchlist and local alert rules.

This store never holds credentials. It keeps a small bounded JSON document at
``~/.config/toss-market/settings.json`` (overridable with ``--settings PATH``),
saved atomically through a sibling temporary file and :func:`os.replace`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .stream import normalize_symbol

SETTINGS_VERSION = 1
MAX_WATCHLIST_ENTRIES = 12
MAX_ALERTS = 100
ALERT_KINDS = ("above", "below", "change-above", "change-below", "volume-spike")

DEFAULT_SETTINGS_PATH = Path.home() / ".config" / "toss-market" / "settings.json"


class SettingsError(RuntimeError):
    """Settings file is malformed, unsafe, or a mutation violates a bound."""


@dataclass(frozen=True, slots=True)
class AlertRule:
    id: str
    symbol: str
    kind: str
    threshold: Decimal
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Settings:
    version: int = SETTINGS_VERSION
    watchlist: tuple[str, ...] = ()
    alerts: tuple[AlertRule, ...] = ()


def parse_threshold(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SettingsError("임계값은 문자열 또는 정수 형태의 양수여야 합니다.")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise SettingsError("임계값을 Decimal로 해석하지 못했습니다.") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SettingsError("임계값은 유한한 양수여야 합니다.")
    return parsed


def next_alert_id(settings: Settings) -> str:
    highest = 0
    for rule in settings.alerts:
        if not rule.id.startswith("A") or not rule.id[1:].isdigit():
            continue
        highest = max(highest, int(rule.id[1:]))
    return f"A{highest + 1}"


def with_watchlist_symbol(settings: Settings, symbol: str) -> tuple[Settings, bool]:
    normalized = normalize_symbol(symbol)
    if normalized in settings.watchlist:
        return settings, False
    if len(settings.watchlist) >= MAX_WATCHLIST_ENTRIES:
        raise SettingsError(f"관심종목은 최대 {MAX_WATCHLIST_ENTRIES}개까지 등록할 수 있습니다.")
    return replace(settings, watchlist=(*settings.watchlist, normalized)), True


def without_watchlist_symbol(settings: Settings, symbol: str) -> Settings:
    normalized = normalize_symbol(symbol)
    if normalized not in settings.watchlist:
        raise SettingsError(f"관심종목에 없는 심볼입니다: {normalized}")
    return replace(
        settings,
        watchlist=tuple(item for item in settings.watchlist if item != normalized),
    )


def with_alert(
    settings: Settings,
    symbol: str,
    kind: str,
    threshold: Any,
    *,
    enabled: bool = True,
) -> tuple[Settings, AlertRule]:
    normalized = normalize_symbol(symbol)
    if kind not in ALERT_KINDS:
        raise SettingsError(f"지원하지 않는 alert kind: {kind}")
    parsed_threshold = parse_threshold(threshold)
    if len(settings.alerts) >= MAX_ALERTS:
        raise SettingsError(f"알림은 최대 {MAX_ALERTS}개까지 등록할 수 있습니다.")
    rule = AlertRule(
        id=next_alert_id(settings),
        symbol=normalized,
        kind=kind,
        threshold=parsed_threshold,
        enabled=enabled,
    )
    return replace(settings, alerts=(*settings.alerts, rule)), rule


def without_alert(settings: Settings, alert_id: str) -> Settings:
    remaining = tuple(rule for rule in settings.alerts if rule.id != alert_id)
    if len(remaining) == len(settings.alerts):
        raise SettingsError(f"존재하지 않는 알림 ID입니다: {alert_id}")
    return replace(settings, alerts=remaining)


def _alert_from_payload(payload: Any) -> AlertRule:
    if not isinstance(payload, dict):
        raise SettingsError("알림 항목은 객체여야 합니다.")
    alert_id = payload.get("id")
    if not isinstance(alert_id, str) or not alert_id.startswith("A") or not alert_id[1:].isdigit():
        raise SettingsError("알림 ID는 A1, A2 … 형식이어야 합니다.")
    try:
        symbol = normalize_symbol(str(payload.get("symbol", "")))
    except ValueError as exc:
        raise SettingsError("알림 심볼 형식이 올바르지 않습니다.") from exc
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise SettingsError(f"지원하지 않는 alert kind: {type(kind).__name__}")
    if kind not in ALERT_KINDS:
        raise SettingsError(f"지원하지 않는 alert kind: {kind}")
    threshold = parse_threshold(payload.get("threshold"))
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SettingsError("알림 enabled 값은 불리언이어야 합니다.")
    return AlertRule(id=alert_id, symbol=symbol, kind=kind, threshold=threshold, enabled=enabled)


def _parse_payload(payload: Any) -> Settings:
    if not isinstance(payload, dict):
        raise SettingsError("설정 JSON은 객체여야 합니다.")
    unexpected = sorted(set(payload) - {"version", "watchlist", "alerts"})
    if unexpected:
        raise SettingsError("설정 파일에 알 수 없는 키가 있습니다.")
    version = payload.get("version", SETTINGS_VERSION)
    if version != SETTINGS_VERSION:
        raise SettingsError(f"지원하지 않는 설정 버전: {version}")

    raw_watchlist = payload.get("watchlist", [])
    if not isinstance(raw_watchlist, list):
        raise SettingsError("watchlist는 배열이어야 합니다.")
    watchlist: list[str] = []
    for item in raw_watchlist:
        if not isinstance(item, str):
            raise SettingsError("watchlist 항목은 문자열 심볼이어야 합니다.")
        try:
            normalized = normalize_symbol(item)
        except ValueError as exc:
            raise SettingsError("watchlist 심볼 형식이 올바르지 않습니다.") from exc
        if normalized in watchlist:
            raise SettingsError(f"watchlist에 중복 심볼이 있습니다: {normalized}")
        watchlist.append(normalized)
    if len(watchlist) > MAX_WATCHLIST_ENTRIES:
        raise SettingsError(f"관심종목은 최대 {MAX_WATCHLIST_ENTRIES}개까지 등록할 수 있습니다.")

    raw_alerts = payload.get("alerts", [])
    if not isinstance(raw_alerts, list):
        raise SettingsError("alerts는 배열이어야 합니다.")
    if len(raw_alerts) > MAX_ALERTS:
        raise SettingsError(f"알림은 최대 {MAX_ALERTS}개까지 등록할 수 있습니다.")
    seen_ids: set[str] = set()
    alerts: list[AlertRule] = []
    for payload_item in raw_alerts:
        rule = _alert_from_payload(payload_item)
        if rule.id in seen_ids:
            raise SettingsError(f"알림 ID가 중복됩니다: {rule.id}")
        seen_ids.add(rule.id)
        alerts.append(rule)

    return Settings(version=SETTINGS_VERSION, watchlist=tuple(watchlist), alerts=tuple(alerts))


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Settings:
        path = self._resolve()
        if not path.exists():
            return Settings()
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsError("설정 파일을 읽지 못했습니다.") from exc
        try:
            payload: Any = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SettingsError("설정 파일이 올바른 JSON이 아닙니다.") from exc
        return _parse_payload(payload)

    def save(self, settings: Settings) -> None:
        if settings.version != SETTINGS_VERSION:
            raise SettingsError(f"지원하지 않는 설정 버전: {settings.version}")
        path = self._resolve()
        parent = path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            current_umask = os.umask(0o077)
            os.umask(current_umask)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(_to_payload(settings), handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(temp_name, path)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise SettingsError("설정 파일을 안전하게 저장하지 못했습니다.") from exc

    def _resolve(self) -> Path:
        path = self.path.expanduser()
        try:
            if path.is_symlink():
                raise SettingsError("설정 파일은 심볼릭 링크일 수 없습니다.")
        except OSError as exc:
            raise SettingsError("설정 파일 상태를 안전하게 확인하지 못했습니다.") from exc
        return path


def _to_payload(settings: Settings) -> dict[str, Any]:
    return {
        "version": settings.version,
        "watchlist": list(settings.watchlist),
        "alerts": [
            {
                "id": rule.id,
                "symbol": rule.symbol,
                "kind": rule.kind,
                "threshold": str(rule.threshold),
                "enabled": rule.enabled,
            }
            for rule in settings.alerts
        ],
    }
