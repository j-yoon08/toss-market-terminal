from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from tests.helpers import sample_snapshot
from toss_market_terminal.models import MarketSnapshot, Price, Trade
from toss_market_terminal.settings import AlertRule, Settings
from toss_market_terminal.stream import StreamStatus, TradeEvent
from toss_market_terminal.tui import TossMarketApp


async def test_wide_tui_renders_three_panel_market_console(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("NVDA", "005930")),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert "TOSS MARKET" in app.query_one("#topbar", Static).render().plain
        assert app.query_one("#watchlist-panel .panel-title", Static).render().plain == "WATCHLIST"
        assert app.query_one("#watchlist", DataTable).row_count == 3
        assert app.query_one("#watchlist", DataTable).max_scroll_x == 0
        assert app.query_one("#orderbook-panel .panel-title", Static).render().plain == "ORDER BOOK"
        assert "MARKET CHART" in app.query_one("#chart-panel .panel-title", Static).render().plain
        assert app.query_one("#trades-panel .panel-title", Static).render().plain == "LIVE TRADES"
        assert app.query_one("#trades", DataTable).max_scroll_x == 0
        assert "체결 평균" in app.query_one("#market-stats", Static).render().plain
        assert not app.screen.has_class("compact")
        await pilot.press("d")
        await pilot.pause()
        assert (
            app.query_one("#chart-panel .panel-title", Static).render().plain
            == "MARKET CHART · DAILY"
        )


async def test_compact_tui_hides_chart_before_squeezing_tables(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert app.screen.has_class("compact")
        assert app.query_one("#orderbook-panel .panel-title", Static).render().plain == "ORDER BOOK"
        assert app.query_one("#trades-panel .panel-title", Static).render().plain == "LIVE TRADES"
        assert app.query_one("#chart-panel").styles.display == "none"
        assert app.query_one("#watchlist-panel").styles.display == "none"


async def test_snapshot_failure_is_sanitized_and_not_marked_live(tmp_path: Path) -> None:
    class FailingClient:
        async def snapshot(self, symbol: str) -> None:
            raise RuntimeError(f"secret-value-for-{symbol}")

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FailingClient()  # type: ignore[assignment]
        assert not await app._refresh_snapshot()
        assert app.connection_state == "ERROR"
        assert "secret-value" not in app.connection_detail
        assert app.connection_detail == "RuntimeError: REST snapshot failed"


async def test_pong_does_not_replace_live_state(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.stream_live = True
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        await app._handle_stream_status(StreamStatus("pong"))
        assert app.stream_live
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


async def test_manual_refresh_restores_live_state(tmp_path: Path) -> None:
    class SuccessfulClient:
        async def snapshot(self, symbol: str) -> MarketSnapshot:
            assert symbol == "AAPL"
            return sample_snapshot()

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = SuccessfulClient()  # type: ignore[assignment]
        app.stream_live = True
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        assert await app._refresh_snapshot()
        assert app.stream_live
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


async def test_live_snapshot_failure_becomes_degraded(tmp_path: Path) -> None:
    class FailingClient:
        async def snapshot(self, symbol: str) -> None:
            raise RuntimeError(f"secret-value-for-{symbol}")

        async def close(self) -> None:
            return None

    class SuccessfulClient:
        async def snapshot(self, symbol: str) -> MarketSnapshot:
            assert symbol == "AAPL"
            return sample_snapshot()

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FailingClient()  # type: ignore[assignment]
        app.stream_live = True
        app.subscription_detail = "topics=2"
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        assert not await app._refresh_snapshot()
        assert app.stream_live
        assert app.connection_state == "DEGRADED"
        assert app.connection_detail == "WS live · REST sync failed"
        assert "secret-value" not in app.connection_detail
        app.client = SuccessfulClient()  # type: ignore[assignment]
        assert await app._refresh_snapshot()
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"
        await app._handle_stream_status(StreamStatus("reconnecting", "retry_in=1.0s"))
        assert not app.stream_live
        assert app.connection_state == "RECONNECTING"


async def test_protocol_error_recovers_on_next_valid_tick(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.stream_live = True
        app.subscription_detail = "topics=2"
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        await app._handle_stream_status(StreamStatus("protocol_error", "invalid-json"))
        assert app.stream_live
        assert app.protocol_degraded
        assert app.connection_state == "DEGRADED"
        app._recover_protocol_status()
        assert not app.protocol_degraded
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


async def test_watchlist_refresh_keeps_last_good_rows_and_primary_state(tmp_path: Path) -> None:
    class PriceClient:
        def __init__(self) -> None:
            self.calls = 0

        async def prices(self, symbols: list[str]) -> dict[str, Price]:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("provider detail must stay hidden")
            return {
                symbol: Price(symbol, Decimal(str(index + 100)), "USD", None)
                for index, symbol in enumerate(symbols)
            }

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("NVDA",)),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = PriceClient()  # type: ignore[assignment]
        app.connection_state = "LIVE"
        app.connection_detail = "topics=2"
        assert await app._refresh_watchlist_prices()
        last_good = dict(app.watchlist_rows)
        assert not await app._refresh_watchlist_prices()
        assert app.watchlist_rows == last_good
        assert app.watchlist_stale
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


async def test_watchlist_navigation_uses_cursor_and_selects_row(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("AAPL", "NVDA", "005930")),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        table = app.query_one("#watchlist", DataTable)
        app.action_watch_down()
        app.action_watch_down()
        assert table.cursor_row == 2
        await app.action_watch_select()
        assert app.symbol == "005930"
        assert app.market == "kr"


def _snapshot_for(symbol: str, price: str, *, market: str, currency: str) -> MarketSnapshot:
    source = sample_snapshot()
    return replace(
        source,
        stock=replace(source.stock, symbol=symbol, name=symbol, market=market, currency=currency),
        price=replace(
            source.price,
            symbol=symbol,
            last_price=Decimal(price),
            currency=currency,
        ),
    )


async def test_switch_cancels_old_feed_recomputes_market_and_ignores_stale_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = asyncio.Event()
    stream_calls: list[tuple[str, str]] = []

    class SnapshotClient:
        async def snapshot(self, symbol: str) -> MarketSnapshot:
            assert symbol == "005930"
            return _snapshot_for("005930", "72000", market="KOSPI", currency="KRW")

        async def close(self) -> None:
            return None

    class FakeStream:
        def __init__(self, _client: object) -> None:
            pass

        async def events(self, symbol: str, market: str):
            stream_calls.append((symbol, market))
            yield TradeEvent(
                "AAPL", Trade(Decimal("999"), Decimal("1"), "2026-08-25T10:00:00Z", "USD")
            )
            yield TradeEvent(
                symbol,
                Trade(Decimal("72001"), Decimal("1"), "2026-08-25T10:00:01Z", "KRW"),
            )
            await asyncio.Event().wait()

    async def old_feed() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("toss_market_terminal.tui.TossMarketStream", FakeStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("AAPL", "005930")),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = SnapshotClient()  # type: ignore[assignment]
        app.feed_task = asyncio.create_task(old_feed())
        await asyncio.sleep(0)
        await app.switch_symbol("005930")
        await pilot.pause()
        assert cancelled.is_set()
        assert app.symbol == "005930"
        assert app.market == "kr"
        assert stream_calls == [("005930", "kr")]
        assert app.current_price == Decimal("72001")
        assert all(trade.price != Decimal("999") for trade in app.trades)


async def test_stale_snapshot_cannot_overwrite_a_concurrent_symbol_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_started = asyncio.Event()
    release_old = asyncio.Event()

    class RacingClient:
        async def snapshot(self, symbol: str) -> MarketSnapshot:
            if symbol == "AAPL":
                old_started.set()
                await release_old.wait()
                return _snapshot_for("AAPL", "111", market="NASDAQ", currency="USD")
            return _snapshot_for("005930", "71000", market="KOSPI", currency="KRW")

        async def close(self) -> None:
            return None

    class QuietStream:
        def __init__(self, _client: object) -> None:
            pass

        async def events(self, symbol: str, market: str):
            _ = (symbol, market)
            await asyncio.Event().wait()
            if False:
                yield StreamStatus("unused")

    monkeypatch.setattr("toss_market_terminal.tui.TossMarketStream", QuietStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = RacingClient()  # type: ignore[assignment]
        old_refresh = asyncio.create_task(app._refresh_snapshot())
        await old_started.wait()
        switching = asyncio.create_task(app.switch_symbol("005930"))
        await pilot.pause()
        release_old.set()
        assert not await old_refresh
        await switching
        assert app.symbol == "005930"
        assert app.snapshot is not None
        assert app.snapshot.stock.symbol == "005930"
        assert app.current_price == Decimal("71000")


async def test_watchlist_refresh_lock_prevents_overlapping_calls(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    concurrent = 0
    maximum = 0

    class SlowClient:
        async def prices(self, symbols: list[str]) -> dict[str, Price]:
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            started.set()
            await release.wait()
            concurrent -= 1
            return {symbol: Price(symbol, Decimal("1"), "USD", None) for symbol in symbols}

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = SlowClient()  # type: ignore[assignment]
        first = asyncio.create_task(app._refresh_watchlist_prices())
        await started.wait()
        assert not await app._refresh_watchlist_prices()
        release.set()
        assert await first
        assert maximum == 1


async def test_tui_emits_one_edge_alert_and_renders_market_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[str] = []
    bells = 0

    def capture_notification(message: str, **_kwargs: object) -> None:
        notifications.append(message)

    def capture_bell() -> None:
        nonlocal bells
        bells += 1

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(
            watchlist=("AAPL",),
            alerts=(AlertRule("A1", "AAPL", "above", Decimal("111")),),
        ),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "notify", capture_notification)
        monkeypatch.setattr(app, "bell", capture_bell)
        stats = app.query_one("#market-stats", Static).render().plain
        assert "시장 신호 요약" in stats
        assert "매수·매도 호가 차이" in stats
        assert "체결 평균" in stats
        assert "호가 매도 우세" in stats
        assert "잔량비" in stats
        assert "체결 상승 우세" in stats
        assert "1분 거래량" in stats
        assert "공개 시세 · 조회 전용" in stats
        assert "BOOK " not in stats
        assert "TICKS " not in stats

        app.current_price = Decimal("112")
        app.current_timestamp = "2026-08-25T10:00:00Z"
        events = app._evaluate_active_alerts()
        assert len(events) == 1
        assert app.latest_alert is not None
        assert app.latest_alert.rule.id == "A1"
        assert notifications and "observed=112" in notifications[0]
        assert bells == 1
        assert (
            "ALERT A1 · AAPL · price > 111 · 112"
            in app.query_one("#statusbar", Static).render().plain
        )

        app.current_price = Decimal("113")
        assert app._evaluate_active_alerts() == ()
        assert bells == 1
