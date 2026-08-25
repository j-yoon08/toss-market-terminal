#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from toss_market_terminal.client import TossMarketClient
from toss_market_terminal.config import DEFAULT_CREDENTIALS_PATH, Credentials
from toss_market_terminal.stream import normalize_symbol
from toss_market_terminal.tui import TossMarketApp


async def capture(
    symbol: str,
    credentials_path: Path,
    output_dir: Path,
    width: int,
    height: int,
) -> Path:
    credentials = Credentials.load(credentials_path)
    async with TossMarketClient(credentials) as client:
        snapshot = await client.snapshot(symbol)
    output_dir.mkdir(parents=True, exist_ok=True)
    app = TossMarketApp(
        symbol,
        credentials_path,
        initial_snapshot=snapshot,
        connect_live=False,
    )
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        filename = f"toss-market-{symbol.lower()}-{width}x{height}.svg"
        return Path(app.save_screenshot(filename=filename, path=str(output_dir)))


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 Toss 시세로 TUI SVG 미리보기 생성")
    parser.add_argument("symbol")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS_PATH)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--width", type=int, default=140)
    parser.add_argument("--height", type=int, default=42)
    args = parser.parse_args()
    if not 80 <= args.width <= 240 or not 24 <= args.height <= 80:
        parser.error("width는 80~240, height는 24~80이어야 합니다.")
    path = asyncio.run(
        capture(
            normalize_symbol(args.symbol),
            args.credentials,
            args.output_dir,
            args.width,
            args.height,
        )
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
