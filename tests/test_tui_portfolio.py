from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Static

from tests.test_portfolio import build_snapshot, official_open_orders_page, official_order
from toss_market_terminal.portfolio import PortfolioScreen
from toss_market_terminal.settings import Settings
from toss_market_terminal.tui import TossMarketApp


def portfolio_app(tmp_path: Path) -> TossMarketApp:
    return TossMarketApp(
        None,
        tmp_path / "unused-credentials.json",
        connect_live=False,
        settings=Settings(watchlist=("005930",)),
        manual_live_orders=True,
        portfolio_refresh_seconds=3600,
    )


async def wait_for_snapshot(app: TossMarketApp, pilot: object) -> None:
    for _ in range(20):
        if app.portfolio_snapshot is not None:
            return
        await pilot.pause()  # type: ignore[attr-defined]
    raise AssertionError("portfolio snapshot did not load")


@pytest.mark.parametrize(
    ("size", "close_key"),
    [((90, 30), "escape"), ((140, 42), "p")],
)
async def test_portfolio_key_opens_responsive_read_only_view_and_closes(
    tmp_path: Path,
    size: tuple[int, int],
    close_key: str,
) -> None:
    snapshot = build_snapshot(
        orders_raw=official_open_orders_page(
            [official_order(side="SELL", quantity="10", execution={"filledQuantity": "3"})]
        )
    )
    app = portfolio_app(tmp_path)
    calls = 0

    async def load_portfolio():
        nonlocal calls
        calls += 1
        return snapshot

    app.portfolio_loader = load_portfolio
    async with app.run_test(size=size) as pilot:
        await pilot.press("p")
        await wait_for_snapshot(app, pilot)

        assert isinstance(app.screen, PortfolioScreen)
        header = app.screen.query_one("#portfolio-header", Static).render().plain
        content = app.screen.query_one("#portfolio-content", Static).render().plain
        body = app.screen.query_one("#portfolio-body")
        rendered = f"{header}\n{content}"
        normalized = " ".join(rendered.split())

        for expected in (
            "*******8901",
            "KRW 매수가능",
            "USD 매수가능",
            "ACCOUNT FRESH",
            "보유 종목",
            "수량 100",
            "매도가능 93",
            "평단",
            "현재가",
            "매입",
            "평가",
            "손익",
            "+8.46%",
            "미체결 주문",
            "총수량 10",
            "체결 3",
            "잔량 7",
            "KRW 평가금액",
            "KRW 평가손익",
        ):
            assert expected in normalized
        assert "bAGzNvM" not in rendered
        assert "12345678901" not in rendered
        assert body.max_scroll_x == 0
        assert calls == 1

        await pilot.press(close_key)
        await pilot.pause()
        assert not isinstance(app.screen, PortfolioScreen)


async def test_portfolio_r_refreshes_account_only_and_preserves_market_state(
    tmp_path: Path,
) -> None:
    app = portfolio_app(tmp_path)
    snapshots = [build_snapshot(krw_power="100"), build_snapshot(krw_power="200")]
    calls = 0

    async def load_portfolio():
        nonlocal calls
        snapshot = snapshots[min(calls, len(snapshots) - 1)]
        calls += 1
        return snapshot

    app.portfolio_loader = load_portfolio
    async with app.run_test(size=(90, 30)) as pilot:
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        await pilot.press("p")
        await wait_for_snapshot(app, pilot)
        await pilot.press("r")
        for _ in range(20):
            if calls >= 2:
                break
            await pilot.pause()
        assert calls == 2
        assert app.portfolio_snapshot is snapshots[1]
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


async def test_portfolio_refresh_lock_blocks_duplicate_concurrent_read(tmp_path: Path) -> None:
    app = portfolio_app(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def load_portfolio():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return build_snapshot()

    app.portfolio_loader = load_portfolio
    first = asyncio.create_task(app._refresh_portfolio())
    await started.wait()
    assert not await app._refresh_portfolio()
    release.set()
    assert await first
    assert calls == 1


async def test_portfolio_failure_keeps_last_good_and_does_not_degrade_market(
    tmp_path: Path,
) -> None:
    app = portfolio_app(tmp_path)
    good = build_snapshot()
    app.portfolio_snapshot = good
    app.connection_state = "LIVE"
    app.connection_detail = "topics=2"

    async def fail_portfolio():
        raise RuntimeError("secret-account-detail")

    app.portfolio_loader = fail_portfolio
    assert not await app._refresh_portfolio()
    assert app.portfolio_snapshot is good
    assert app.portfolio_stale
    assert app.portfolio_error == "RuntimeError: REST snapshot failed"
    assert "secret-account-detail" not in app.portfolio_error
    assert app.connection_state == "LIVE"
    assert app.connection_detail == "topics=2"


async def test_portfolio_polling_starts_once_and_is_cancelled_on_unmount(tmp_path: Path) -> None:
    app = portfolio_app(tmp_path)
    calls = 0

    async def load_portfolio():
        nonlocal calls
        calls += 1
        return build_snapshot()

    app.portfolio_loader = load_portfolio
    async with app.run_test(size=(90, 30)) as pilot:
        app._maybe_start_portfolio_polling()
        task = app.portfolio_task
        assert task is not None
        app._maybe_start_portfolio_polling()
        assert app.portfolio_task is task
        for _ in range(20):
            if calls:
                break
            await pilot.pause()
        assert calls >= 1

    assert task.done()
    assert task.cancelled()


async def test_account_seq_is_shared_by_portfolio_and_order_context(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused-credentials.json",
        connect_live=False,
        initial_snapshot=None,
        account_seq=7,
    )
    calls: list[tuple[str, object]] = []
    portfolio = build_snapshot()

    class FakeClient:
        async def portfolio_snapshot(self, account_seq: int | None):
            calls.append(("portfolio", account_seq))
            return portfolio

        async def account_context(self, symbol: str, account_seq: int | None):
            calls.append(("context", (symbol, account_seq)))
            return object()

    app.client = FakeClient()  # type: ignore[assignment]
    assert await app._load_portfolio_snapshot() is portfolio
    await app._load_account_context("AAPL")
    assert calls == [("portfolio", 7), ("context", ("AAPL", 7))]
