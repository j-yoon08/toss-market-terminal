from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import Static

from tests.helpers import sample_snapshot as _sample_snapshot
from tests.test_portfolio import (
    build_snapshot,
    official_item,
    official_open_orders_page,
    official_order,
    official_overview,
)
from tests.test_portfolio_phase2 import (
    official_closed_page,
    official_exchange_rate,
)
from toss_market_terminal.models import ClosedOrdersPage, ExchangeRate
from toss_market_terminal.portfolio import PortfolioScreen
from toss_market_terminal.settings import Settings
from toss_market_terminal.tui import TossMarketApp


def sample_snapshot():
    return _sample_snapshot(fresh_price=True)


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


def chart_overlay_app(tmp_path: Path) -> TossMarketApp:
    """A ``connect_live=False`` app with a market snapshot already loaded for AAPL (USD)."""
    return TossMarketApp(
        "AAPL",
        tmp_path / "unused-credentials.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("AAPL",)),
        manual_live_orders=True,
        portfolio_refresh_seconds=3600,
        exchange_rate_refresh_seconds=3600,
        order_history_refresh_seconds=3600,
    )


def _held_snapshot(**item_overrides: object):
    item = official_item(
        symbol="AAPL",
        name="Apple Inc",
        marketCountry="US",
        currency="USD",
        quantity="10",
        lastPrice="110",
        averagePurchasePrice="90",
    )
    item.update(item_overrides)
    return build_snapshot(holdings_raw=official_overview([item]))


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
    app.portfolio_snapshot = build_snapshot()
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


async def test_inflight_history_is_discarded_when_portfolio_account_changes(
    tmp_path: Path,
) -> None:
    app = portfolio_app(tmp_path)
    old_snapshot = build_snapshot()
    new_snapshot = replace(
        old_snapshot,
        account=replace(old_snapshot.account, account_seq=2),
    )
    old_page = ClosedOrdersPage.from_api(official_closed_page())
    new_page = ClosedOrdersPage.from_api(official_closed_page([]))
    app.portfolio_snapshot = old_snapshot
    app.closed_orders = old_page
    app.order_history_account_seq = old_snapshot.account.account_seq
    started = asyncio.Event()
    release = asyncio.Event()
    history_calls = 0

    async def delayed_history():
        nonlocal history_calls
        history_calls += 1
        if history_calls == 1:
            started.set()
            await release.wait()
            return old_page
        return new_page

    async def load_new_portfolio():
        return new_snapshot

    app.closed_orders_loader = delayed_history
    app.portfolio_loader = load_new_portfolio
    history_task = asyncio.create_task(app._run_order_history_polling())
    app.order_history_task = history_task
    try:
        await started.wait()
        assert await app._refresh_portfolio()
        assert app.closed_orders is None
        assert app.order_history_account_seq is None
        assert app.order_history_wakeup.is_set()
        release.set()
        for _ in range(30):
            if app.closed_orders is new_page:
                break
            await asyncio.sleep(0.01)
        assert app.closed_orders is new_page
        assert app.order_history_account_seq == new_snapshot.account.account_seq
        assert history_calls == 2
    finally:
        history_task.cancel()
        await asyncio.gather(history_task, return_exceptions=True)


async def test_open_portfolio_marks_fx_stale_when_validity_expires_without_refetch(
    tmp_path: Path,
) -> None:
    app = portfolio_app(tmp_path)
    now = datetime.now(UTC)
    exchange_calls = 0

    async def load_portfolio():
        return build_snapshot()

    async def load_exchange():
        nonlocal exchange_calls
        exchange_calls += 1
        return ExchangeRate.from_api(
            official_exchange_rate(
                validFrom=(now - timedelta(minutes=1)).isoformat(),
                validUntil=(now + timedelta(seconds=2)).isoformat(),
            )
        )

    async def load_history():
        return ClosedOrdersPage.from_api(official_closed_page())

    app.portfolio_loader = load_portfolio
    app.exchange_rate_loader = load_exchange
    app.closed_orders_loader = load_history
    async with app.run_test(size=(90, 30)) as pilot:
        await wait_for_snapshot(app, pilot)
        await wait_for_insights(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        content = app.screen.query_one("#portfolio-content", Static)
        assert "FX FRESH" in content.render().plain
        await asyncio.sleep(2.1)
        await pilot.pause()
        assert "FX STALE" in content.render().plain
        assert exchange_calls == 1


# --- active held-symbol average-price chart overlay ------------------------


async def test_holding_average_overlay_shows_for_active_held_symbol(tmp_path: Path) -> None:
    app = chart_overlay_app(tmp_path)

    async def load_portfolio():
        return _held_snapshot()

    app.portfolio_loader = load_portfolio
    async with app.run_test(size=(140, 42)) as pilot:
        await wait_for_snapshot(app, pilot)
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단 90" in content
        assert "STALE" not in content


async def test_holding_average_overlay_absent_without_portfolio_snapshot(tmp_path: Path) -> None:
    app = chart_overlay_app(tmp_path)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단" not in content


async def test_holding_average_overlay_removed_on_switch_to_non_held_symbol_and_restored(
    tmp_path: Path,
) -> None:
    app = chart_overlay_app(tmp_path)
    app.portfolio_snapshot = _held_snapshot()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단 90" in content

        other = sample_snapshot()
        other_snapshot = replace(
            other,
            stock=replace(other.stock, symbol="TSLA"),
            price=replace(other.price, symbol="TSLA"),
        )
        app._apply_snapshot(other_snapshot)
        app._render_chart()
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단" not in content  # previous symbol's line does not linger

        app._apply_snapshot(sample_snapshot())
        app._render_chart()
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단 90" in content


@pytest.mark.parametrize(
    "item_overrides",
    [
        {"currency": "KRW"},  # currency mismatch vs. the chart's USD
        {"quantity": "0"},  # zero quantity
        {"averagePurchasePrice": "0"},  # zero average purchase price
    ],
)
async def test_holding_average_overlay_fails_closed_on_mismatch_or_zero_values(
    tmp_path: Path, item_overrides: dict[str, object]
) -> None:
    app = chart_overlay_app(tmp_path)
    app.portfolio_snapshot = _held_snapshot(**item_overrides)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단" not in content


async def test_holding_average_overlay_no_holding_for_symbol_is_absent(tmp_path: Path) -> None:
    app = chart_overlay_app(tmp_path)
    app.portfolio_snapshot = _held_snapshot(symbol="NVDA")
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단" not in content


async def test_holding_average_overlay_refresh_failure_retains_stale_and_recovery_clears_it(
    tmp_path: Path,
) -> None:
    app = chart_overlay_app(tmp_path)
    good = _held_snapshot()
    app.portfolio_snapshot = good

    async def fail_portfolio():
        raise RuntimeError("secret-account-detail")

    # No portfolio_loader is set yet, so _maybe_start_portfolio_polling (started
    # implicitly on mount) stays a no-op and cannot race the assertions below.
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단 90" in content
        assert "STALE" not in content

        app.portfolio_loader = fail_portfolio
        assert not await app._refresh_portfolio()
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단 90 STALE" in content
        assert app.portfolio_snapshot is good  # last-good retained, not cleared
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"

        async def recover_portfolio():
            return good

        app.portfolio_loader = recover_portfolio
        assert await app._refresh_portfolio()
        await pilot.pause()
        content = app.query_one("#chart-content", Static).render().plain
        assert "보유 평단 90" in content
        assert "STALE" not in content
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


@pytest.mark.parametrize("size", [(90, 30), (140, 42)])
async def test_holding_average_overlay_stays_within_chart_width(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = chart_overlay_app(tmp_path)
    app.portfolio_snapshot = _held_snapshot()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        chart = app.query_one("#chart-content", Static)
        chart_panel = app.query_one("#chart-panel")
        content = chart.render().plain
        if chart_panel.styles.display != "none":
            width = chart.content_size.width
            assert width > 0
            for line in content.splitlines():
                assert cell_len(line) <= width
