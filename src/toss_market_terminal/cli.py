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
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="현재가·호가·체결·1분봉 스냅샷")
    snapshot.add_argument("symbol")
    snapshot.add_argument("--json", action="store_true", dest="json_output")

    watch = subparsers.add_parser("watch", help="실시간 Textual TUI 실행")
    watch.add_argument("symbol")

    probe = subparsers.add_parser("probe", help="REST + WebSocket 조회 전용 연결 검증")
    probe.add_argument("symbol")
    probe.add_argument("--seconds", type=float, default=8.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        symbol = normalize_symbol(args.symbol)
        if args.command == "snapshot":
            return asyncio.run(run_snapshot(symbol, args.credentials, args.json_output))
        if args.command == "probe":
            if not 1 <= args.seconds <= 60:
                raise ValueError("probe 시간은 1~60초여야 합니다.")
            return asyncio.run(run_probe(symbol, args.credentials, args.seconds))
        if args.command == "watch":
            TossMarketApp(symbol, args.credentials).run()
            return 0
    except (CredentialError, TossApiError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
