from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from rich.console import Console

from .client import TossApiError, TossMarketClient
from .config import DEFAULT_CREDENTIALS_PATH, CredentialError, Credentials
from .render import snapshot_renderable
from .settings import (
    DEFAULT_SETTINGS_PATH,
    SettingsError,
    SettingsStore,
    with_alert,
    with_watchlist_symbol,
    without_alert,
    without_watchlist_symbol,
)
from .stream import StreamStatus, TossMarketStream, infer_market, normalize_symbol
from .tui import TossMarketApp


def json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"지원하지 않는 JSON 값: {type(value).__name__}")


async def run_snapshot(symbol: str, credentials_path: Path, json_output: bool) -> int:
    credentials = Credentials.load(credentials_path)
    async with TossMarketClient(credentials) as client:
        snapshot = await client.snapshot(symbol)
    if json_output:
        print(json.dumps(asdict(snapshot), ensure_ascii=False, default=json_default))
    else:
        Console().print(snapshot_renderable(snapshot))
    return 0


async def run_probe(symbol: str, credentials_path: Path, seconds: float) -> int:
    credentials = Credentials.load(credentials_path)
    async with TossMarketClient(credentials) as client:
        snapshot = await client.snapshot(symbol)
        stream = TossMarketStream(client)
        subscribed = False
        live_messages = 0

        async def collect() -> None:
            nonlocal subscribed, live_messages
            async for event in stream.events(symbol, infer_market(symbol)):
                if (
                    isinstance(event, StreamStatus)
                    and event.state == "subscribed"
                    and event.detail == "topics=2"
                ):
                    subscribed = True
                elif not isinstance(event, StreamStatus):
                    live_messages += 1
                if subscribed and live_messages > 0:
                    return

        try:
            await asyncio.wait_for(collect(), timeout=seconds)
        except TimeoutError:
            pass

    result = {
        "ok": subscribed,
        "scope": "public_market_data_read_only",
        "symbol": symbol,
        "market": snapshot.stock.market,
        "currency": snapshot.price.currency,
        "rest_snapshot": True,
        "websocket_subscription_ack": subscribed,
        "live_message_received": live_messages > 0,
        "live_message_count": live_messages,
        "account_endpoints_called": False,
        "order_endpoints_called": False,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if subscribed else 2


def run_watchlist_list(settings_path: Path) -> int:
    settings = SettingsStore(settings_path).load()
    print("SYMBOL\tALERTS")
    for symbol in settings.watchlist:
        active = sum(1 for rule in settings.alerts if rule.symbol == symbol and rule.enabled)
        print(f"{symbol}\t{active}")
    return 0


def run_watchlist_add(settings_path: Path, symbol: str) -> int:
    normalized = normalize_symbol(symbol)
    store = SettingsStore(settings_path)
    settings = store.load()
    updated, created = with_watchlist_symbol(settings, normalized)
    if created:
        store.save(updated)
        print(f"ADDED\t{normalized}")
    else:
        print(f"EXISTS\t{normalized}")
    return 0


def run_watchlist_remove(settings_path: Path, symbol: str) -> int:
    normalized = normalize_symbol(symbol)
    store = SettingsStore(settings_path)
    settings = store.load()
    updated = without_watchlist_symbol(settings, normalized)
    store.save(updated)
    print(f"REMOVED\t{normalized}")
    return 0


def run_alert_list(settings_path: Path) -> int:
    settings = SettingsStore(settings_path).load()
    print("ID\tSYMBOL\tKIND\tTHRESHOLD\tENABLED")
    for rule in settings.alerts:
        enabled = "true" if rule.enabled else "false"
        print(f"{rule.id}\t{rule.symbol}\t{rule.kind}\t{rule.threshold}\t{enabled}")
    return 0


def run_alert_add(settings_path: Path, symbol: str, kind: str, threshold: str) -> int:
    normalized = normalize_symbol(symbol)
    store = SettingsStore(settings_path)
    settings = store.load()
    updated, created = with_alert(settings, normalized, kind, threshold)
    store.save(updated)
    print(f"ADDED\t{created.id}")
    return 0


def run_alert_remove(settings_path: Path, alert_id: str) -> int:
    normalized_id = alert_id.strip().upper()
    store = SettingsStore(settings_path)
    settings = store.load()
    updated = without_alert(settings, normalized_id)
    store.save(updated)
    print(f"REMOVED\t{normalized_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toss-market",
        description="토스증권 공식 Open API 기반 조회 전용 실시간 주식 터미널",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_PATH,
        help=f"자격증명 경로 (기본값: {DEFAULT_CREDENTIALS_PATH})",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        dest="settings_path",
        default=DEFAULT_SETTINGS_PATH,
        help=f"설정 파일 경로 (기본값: {DEFAULT_SETTINGS_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="현재가·호가·체결·1분봉 스냅샷")
    snapshot.add_argument("symbol")
    snapshot.add_argument("--json", action="store_true", dest="json_output")

    watch = subparsers.add_parser("watch", help="실시간 Textual TUI 실행")
    watch.add_argument("symbol")

    probe = subparsers.add_parser("probe", help="REST + WebSocket 조회 전용 연결 검증")
    probe.add_argument("symbol")
    probe.add_argument("--seconds", type=float, default=8.0)

    watchlist = subparsers.add_parser("watchlist", help="관심종목 관리 (설정 파일 기반)")
    watchlist.add_argument("--settings", type=Path, dest="settings", default=None)
    watchlist_sub = watchlist.add_subparsers(dest="watchlist_command", required=True)
    watchlist_sub.add_parser("list")
    watchlist_add = watchlist_sub.add_parser("add")
    watchlist_add.add_argument("symbol")
    watchlist_remove = watchlist_sub.add_parser("remove")
    watchlist_remove.add_argument("symbol")

    alert = subparsers.add_parser("alert", help="로컬 알림 규칙 관리 (설정 파일 기반)")
    alert.add_argument("--settings", type=Path, dest="settings", default=None)
    alert_sub = alert.add_subparsers(dest="alert_command", required=True)
    alert_sub.add_parser("list")
    alert_add = alert_sub.add_parser("add")
    alert_add.add_argument("symbol")
    alert_add.add_argument(
        "kind", choices=["above", "below", "change-above", "change-below", "volume-spike"]
    )
    alert_add.add_argument("threshold")
    alert_remove = alert_sub.add_parser("remove")
    alert_remove.add_argument("id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def settings_file() -> Path:
        return getattr(args, "settings", None) or args.settings_path

    try:
        if args.command in {"snapshot", "probe"}:
            symbol = normalize_symbol(args.symbol)
            if args.command == "snapshot":
                return asyncio.run(run_snapshot(symbol, args.credentials, args.json_output))
            if not 1 <= args.seconds <= 60:
                raise ValueError("probe 시간은 1~60초여야 합니다.")
            return asyncio.run(run_probe(symbol, args.credentials, args.seconds))
        if args.command == "watch":
            symbol = normalize_symbol(args.symbol)
            result = TossMarketApp(symbol, args.credentials, settings_path=settings_file()).run()
            return result or 0
        if args.command == "watchlist":
            if args.watchlist_command == "list":
                return run_watchlist_list(settings_file())
            if args.watchlist_command == "add":
                return run_watchlist_add(settings_file(), args.symbol)
            if args.watchlist_command == "remove":
                return run_watchlist_remove(settings_file(), args.symbol)
        if args.command == "alert":
            if args.alert_command == "list":
                return run_alert_list(settings_file())
            if args.alert_command == "add":
                return run_alert_add(settings_file(), args.symbol, args.kind, args.threshold)
            if args.alert_command == "remove":
                return run_alert_remove(settings_file(), args.id)
    except (CredentialError, TossApiError, SettingsError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
