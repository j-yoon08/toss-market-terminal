from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from rich.console import Console

from .client import TossApiError, TossMarketClient
from .config import DEFAULT_CREDENTIALS_PATH, CredentialError, Credentials
from .live_order import MANUAL_LIVE_ENV_KEY, MANUAL_LIVE_ENV_VALUE
from .models import AccountContext
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


def account_context_payload(context: AccountContext) -> dict[str, object]:
    """Privacy-safe JSON shape: masked account number only, explicit boundary."""
    payload = asdict(context)
    # Defense in depth: the raw account number is never stored on any model.
    # Enforce the masking shape (all but the last 4 chars are '*') so a future
    # model change cannot leak it into CLI JSON unnoticed.
    masked = payload["account"]["masked_account_no"]
    if len(masked) < 4 or masked[:-4] != "*" * (len(masked) - 4):
        raise ValueError("계좌번호가 마스킹되지 않았습니다.")
    return payload


async def run_account(
    symbol: str,
    credentials_path: Path,
    account_seq: int | None,
    json_output: bool,
) -> int:
    credentials = Credentials.load(credentials_path)
    async with TossMarketClient(credentials) as client:
        context = await client.account_context(symbol, account_seq)
    if json_output:
        payload = account_context_payload(context)
        payload.setdefault("scope", "account_read_only")
        print(json.dumps(payload, ensure_ascii=False, default=json_default))
        return 0
    console = Console()
    item = context.holding
    console.print(
        f"[bold]계좌[/bold] {context.account.masked_account_no} "
        f"({context.account.account_type}, seq={context.account.account_seq})"
    )
    console.print(f"[bold]심볼[/bold] {context.symbol}")
    if item is None:
        console.print("[dim]보유 정보 없음 (보유 수량 0으로 조회)[/dim]")
    else:
        console.print(
            f"보유 수량 {item.quantity} · 평단 {item.average_purchase_price} "
            f"{item.currency} · 평가액 {item.market_value.amount} {item.currency}"
        )
        rate_pct = item.profit_loss.rate_after_cost * 100
        console.print(f"평가손익 {item.profit_loss.amount} ({rate_pct:.2f}%)")
    power = context.buying_power
    console.print(f"[bold]매수가능금액[/bold] {power.cash_buying_power} {power.currency}")
    console.print("[dim]scope=account_read_only · 주문 API 호출 없음[/dim]")
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
        description="토스증권 공식 Open API 기반 PAPER 기본 실시간 주식 터미널",
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

    account = subparsers.add_parser(
        "account", help="계좌·보유·매수가능금액 조회 (읽기 전용, 계좌번호 마스킹)"
    )
    account.add_argument("symbol")
    account.add_argument("--account-seq", type=int, default=None, dest="account_seq")
    account.add_argument("--json", action="store_true", dest="json_output")

    watch = subparsers.add_parser(
        "watch", help="실시간 Textual TUI 실행 (기본 PAPER, 선택적 수동 LIVE)"
    )
    watch.add_argument("symbol")
    watch.add_argument(
        "--live-orders",
        action="store_true",
        help="수동 LIVE 승인 화면 활성화(환경 게이트와 주문별 최종 확인도 필요)",
    )
    watch.add_argument(
        "--account-seq",
        type=int,
        default=None,
        dest="account_seq",
        help="포트폴리오/계좌 컨텍스트에 사용할 계좌 식별자(생략 시 자동 선택)",
    )

    live = subparsers.add_parser(
        "live",
        help="수동 LIVE TUI 실행(앱에서 관심종목 선택, 주문마다 최종 승인)",
    )
    live.add_argument("symbol", nargs="?", help="선택적 시작 종목(생략 시 앱에서 선택)")
    live.add_argument(
        "--account-seq",
        type=int,
        default=None,
        dest="account_seq",
        help="포트폴리오/계좌 컨텍스트와 주문 계좌에 사용할 계좌 식별자(생략 시 자동 선택)",
    )

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
        if args.command in {"snapshot", "account", "probe"}:
            symbol = normalize_symbol(args.symbol)
            if args.command == "snapshot":
                return asyncio.run(run_snapshot(symbol, args.credentials, args.json_output))
            if args.command == "account":
                if args.account_seq is not None and args.account_seq <= 0:
                    raise ValueError("--account-seq는 양의 정수여야 합니다.")
                return asyncio.run(
                    run_account(symbol, args.credentials, args.account_seq, args.json_output)
                )
            if not 1 <= args.seconds <= 60:
                raise ValueError("probe 시간은 1~60초여야 합니다.")
            return asyncio.run(run_probe(symbol, args.credentials, args.seconds))
        if args.command in {"watch", "live"}:
            live_shortcut = args.command == "live"
            symbol = normalize_symbol(args.symbol) if args.symbol is not None else None
            if args.account_seq is not None and args.account_seq <= 0:
                raise ValueError("--account-seq는 양의 정수여야 합니다.")
            previous_gate = os.environ.get(MANUAL_LIVE_ENV_KEY)
            if live_shortcut:
                os.environ[MANUAL_LIVE_ENV_KEY] = MANUAL_LIVE_ENV_VALUE
            try:
                result = TossMarketApp(
                    symbol,
                    args.credentials,
                    settings_path=settings_file(),
                    manual_live_orders=live_shortcut or getattr(args, "live_orders", False),
                    account_seq=args.account_seq,
                ).run()
                return result or 0
            finally:
                if live_shortcut:
                    if previous_gate is None:
                        os.environ.pop(MANUAL_LIVE_ENV_KEY, None)
                    else:
                        os.environ[MANUAL_LIVE_ENV_KEY] = previous_gate
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
