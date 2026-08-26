from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Static

from tests.test_portfolio import build_snapshot, official_open_orders_page, official_order
from tests.test_portfolio_phase2 import (
    official_closed_page,
    official_exchange_rate,
)
from toss_market_terminal.models import ClosedOrdersPage, ExchangeRate
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
        exchange_rate_refresh_seconds=3600,
        order_history_refresh_seconds=3600,
    )


async def wait_for_snapshot(app: TossMarketApp, pilot: object) -> None:
    for _ in range(20):
        if app.portfolio_snapshot is not None:
            return
        await pilot.pause()  # type: ignore[attr-defined]
    raise AssertionError("portfolio snapshot did not load")


async def wait_for_insights(app: TossMarketApp, pilot: object) -> None:
    for _ in range(30):
        if app.exchange_rate is not None and app.closed_orders is not None:
            return
        await pilot.pause()  # type: ignore[attr-defined]
    raise AssertionError("portfolio insights did not load")


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


@pytest.mark.parametrize("size", [(90, 30), (140, 42)])
async def test_phase2_insights_render_and_refresh_independently(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = portfolio_app(tmp_path)
    calls = {"portfolio": 0, "exchange": 0, "history": 0}

    async def load_portfolio():
        calls["portfolio"] += 1
        return build_snapshot()

    async def load_exchange():
        calls["exchange"] += 1
        return ExchangeRate.from_api(official_exchange_rate())

    async def load_history():
        calls["history"] += 1
        return ClosedOrdersPage.from_api(official_closed_page())

    app.portfolio_loader = load_portfolio
    app.exchange_rate_loader = load_exchange
    app.closed_orders_loader = load_history

    async with app.run_test(size=size) as pilot:
        await wait_for_snapshot(app, pilot)
        await wait_for_insights(app, pilot)
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PortfolioScreen)
        content = app.screen.query_one("#portfolio-content", Static).render().plain
        body = app.screen.query_one("#portfolio-body")
        for expected in (
            "비중 100.0%",
            "오늘손익",
            "매매기준율",
            "환산 평가액",
            "최근 종료 주문",
            "평균체결",
            "공식 API 미제공",
        ):
            assert expected in content
        assert "raw-order-id-must-never-render" not in content
        assert body.max_scroll_x == 0
        before = calls.copy()
        await pilot.press("r")
        for _ in range(30):
            if all(calls[key] > before[key] for key in calls):
                break
            await pilot.pause()
        assert calls == {key: before[key] + 1 for key in before}
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"
        exchange_task = app.exchange_rate_task
        history_task = app.order_history_task
        assert exchange_task is not None
        assert history_task is not None

    assert exchange_task.done() and exchange_task.cancelled()
    assert history_task.done() and history_task.cancelled()


async def test_phase2_failures_keep_last_good_and_do_not_degrade_market(tmp_path: Path) -> None:
    app = portfolio_app(tmp_path)
    good_exchange = ExchangeRate.from_api(official_exchange_rate())
    good_history = ClosedOrdersPage.from_api(official_closed_page())
    app.exchange_rate = good_exchange
    app.closed_orders = good_history
    app.connection_state = "LIVE"
    app.connection_detail = "topics=2"

    async def fail_exchange():
        raise RuntimeError("secret-fx-payload")

    async def fail_history():
        raise RuntimeError("secret-order-payload")

    app.exchange_rate_loader = fail_exchange
    app.closed_orders_loader = fail_history
    assert not await app._refresh_exchange_rate()
    assert not await app._refresh_order_history()
    assert app.exchange_rate is good_exchange
    assert app.closed_orders is good_history
    assert app.exchange_rate_stale
    assert app.order_history_stale
    assert app.exchange_rate_error == "RuntimeError: REST snapshot failed"
    assert app.order_history_error == "RuntimeError: REST snapshot failed"
    assert "secret" not in app.exchange_rate_error
    assert "secret" not in app.order_history_error
    assert app.connection_state == "LIVE"
    assert app.connection_detail == "topics=2"


async def test_closed_order_history_reuses_selected_portfolio_account(tmp_path: Path) -> None:
    app = portfolio_app(tmp_path)
    app.portfolio_snapshot = build_snapshot()
    calls: list[tuple[int, object, object, int]] = []
    expected = ClosedOrdersPage.from_api(official_closed_page())

    class FakeClient:
        async def closed_orders(
            self,
            account_seq: int,
            *,
            start_date: object,
            end_date: object,
            limit: int,
        ) -> ClosedOrdersPage:
            calls.append((account_seq, start_date, end_date, limit))
            return expected

    app.client = FakeClient()  # type: ignore[assignment]
    assert await app._load_closed_orders() is expected
    assert len(calls) == 1
    account_seq, start_date, end_date, limit = calls[0]
    assert account_seq == app.portfolio_snapshot.account.account_seq
    assert (end_date - start_date).days == 29  # type: ignore[operator]
    assert limit == 20
