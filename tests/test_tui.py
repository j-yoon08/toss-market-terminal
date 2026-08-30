from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, Static

from tests.helpers import sample_snapshot as _sample_snapshot
from toss_market_terminal import tui as tui_module
from toss_market_terminal.models import MarketSnapshot, Price, Trade
from toss_market_terminal.render import (
    CHART_MODE_LABELS,
    TIMEFRAME_LABELS_KO,
    nearest_support_resistance,
)
from toss_market_terminal.render import chart_indicator_base as real_chart_indicator_base
from toss_market_terminal.settings import AlertRule, Settings, SettingsStore
from toss_market_terminal.stream import OrderbookEvent, StreamStatus, TradeEvent
from toss_market_terminal.tui import TossMarketApp, WatchlistAddScreen


def sample_snapshot() -> MarketSnapshot:
    return _sample_snapshot(fresh_price=True)


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
        watchlist = app.query_one("#watchlist", DataTable)
        assert watchlist.row_count == 3
        assert watchlist.max_scroll_x == 0
        symbols = [watchlist.get_cell_at(Coordinate(row, 0)) for row in range(watchlist.row_count)]
        assert symbols.count(">AAPL") == 1
        assert sum(symbol.startswith(">") for symbol in symbols) == 1
        prices = [
            watchlist.get_cell_at(Coordinate(row, 1)).plain for row in range(watchlist.row_count)
        ]
        assert all(price.startswith(" ") for price in prices)
        assert app.query_one("#orderbook-panel .panel-title", Static).render().plain == "ORDER BOOK"
        assert "MARKET CHART" in app.query_one("#chart-panel .panel-title", Static).render().plain
        assert app.query_one("#trades-panel .panel-title", Static).render().plain == "LIVE TRADES"
        assert app.query_one("#trades", DataTable).max_scroll_x == 0
        assert "시장 해석" in app.query_one("#market-stats", Static).render().plain
        assert not app.screen.has_class("compact")
        widths = {
            panel: app.query_one(f"#{panel}-panel").size.width
            for panel in ("watchlist", "orderbook", "chart", "trades")
        }
        total_width = sum(widths.values())
        assert widths["watchlist"] / total_width == pytest.approx(0.15, abs=0.02)
        assert widths["orderbook"] / total_width == pytest.approx(0.24, abs=0.02)
        assert widths["chart"] / total_width == pytest.approx(0.42, abs=0.02)
        assert widths["trades"] / total_width == pytest.approx(0.19, abs=0.02)
        assert app.query_one("#summary").styles.height.value == 4
        assert app.query_one("#statusbar").styles.height.value == 2
        await pilot.press("5")
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


async def test_live_trade_time_hides_fractional_seconds(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(Trade(Decimal("110"), Decimal("2"), "2026-08-25T10:00:00.987Z", "USD"),),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        table = app.query_one("#trades", DataTable)
        assert table.get_cell_at(Coordinate(0, 0)) == "10:00:00"


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
        app.last_sync_monotonic = 123.0
        assert not await app._refresh_snapshot()
        assert app.connection_state == "ERROR"
        assert "secret-value" not in app.connection_detail
        assert app.connection_detail == "RuntimeError: REST snapshot failed"
        assert app.last_sync_monotonic == 123.0


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
        assert app.last_sync_monotonic is not None
        assert "SYNC" in app.query_one("#statusbar", Static).render().plain


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


async def test_recover_protocol_status_stays_degraded_while_indicator_degraded(
    tmp_path: Path,
) -> None:
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
        app.protocol_degraded = True
        app.indicator_degraded = True
        app.connection_state = "DEGRADED"
        app.connection_detail = "ValueError: 지표 계산 실패"
        app._recover_protocol_status()
        assert app.connection_state == "DEGRADED"
        assert app.connection_detail == "ValueError: 지표 계산 실패"
        assert app.protocol_degraded
        assert app.indicator_degraded


async def test_orderbook_event_does_not_refresh_tick_but_trade_event_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class OrderbookOnlyStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield OrderbookEvent(symbol, sample_snapshot().orderbook)

    monkeypatch.setattr(tui_module, "TossMarketStream", OrderbookOnlyStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FakeClient()  # type: ignore[assignment]
        tick_after_mount = app.last_tick_monotonic
        book_after_mount = app.last_orderbook_monotonic
        assert tick_after_mount is not None
        # The initial REST snapshot establishes both provider-anchored clocks.
        assert book_after_mount is not None

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        # Orderbook-only traffic must never renew the TICK (price) clock.
        assert app.last_tick_monotonic == tick_after_mount
        assert app.last_orderbook_monotonic is not None
        status = app.query_one("#statusbar", Static).render().plain
        assert "BOOK " in status


async def test_trade_event_renews_tick_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class OneTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("150"), Decimal("7"), "2026-08-25T10:00:30+09:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", OneTradeStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T01:00:30+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FakeClient()  # type: ignore[assignment]
        app.last_tick_monotonic = 1.0  # force a known-stale sentinel

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        assert app.last_tick_monotonic is not None
        assert app.last_tick_monotonic != 1.0


async def test_hour_old_trade_event_never_freshens_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class OneTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("999"), Decimal("7"), "2026-08-25T10:00:00+00:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", OneTradeStream)
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:59:55+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T11:00:00+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FakeClient()  # type: ignore[assignment]
        tick_before = app.last_tick_monotonic

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        assert app.current_price != Decimal("999")
        assert app.current_timestamp != "2026-08-25T10:00:00+00:00"
        assert app.last_tick_monotonic == tick_before


async def test_out_of_order_trade_event_cannot_regress_current_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class TwoTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("151"), Decimal("1"), "2026-08-25T10:59:55+00:00", "USD"),
            )
            yield TradeEvent(
                symbol,
                Trade(Decimal("999"), Decimal("1"), "2026-08-25T10:59:00+00:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", TwoTradeStream)
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:59:50+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T11:00:00+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FakeClient()  # type: ignore[assignment]

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        assert app.current_price == Decimal("151")
        assert app.current_timestamp == "2026-08-25T10:59:55+00:00"
        tick_after_first = app._tick_monotonic_for_timestamp("2026-08-25T10:59:55+00:00")
        assert app.last_tick_monotonic == pytest.approx(tick_after_first, abs=1.0)


async def test_small_future_skew_trade_event_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class OneTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("151"), Decimal("1"), "2026-08-25T11:00:10+00:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", OneTradeStream)
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:59:55+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T11:00:00+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = FakeClient()  # type: ignore[assignment]

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        assert app.current_price == Decimal("151")
        assert app.current_timestamp == "2026-08-25T11:00:10+00:00"
        assert app.last_tick_monotonic is not None


async def test_hour_old_tick_makes_interpretation_stale_even_when_live(
    tmp_path: Path,
) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        app.last_tick_monotonic = tui_module.time.monotonic()
        assert not app._interpretation_is_stale()
        app.last_tick_monotonic = tui_module.time.monotonic() - 3600.0
        assert app._interpretation_is_stale()


def test_price_stale_seconds_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TossMarketApp(
            "AAPL",
            tmp_path / "unused.json",
            connect_live=False,
            price_stale_seconds=0,
        )


def _fixed_utc_now(iso: str):
    from datetime import datetime as _datetime

    fixed = _datetime.fromisoformat(iso)

    def _now() -> _datetime:
        return fixed

    return _now


async def test_old_snapshot_timestamp_is_immediately_stale(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:00:00+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T11:00:00+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        assert app._interpretation_is_stale()


async def test_fresh_snapshot_timestamp_is_not_stale(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:00:00+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T10:00:05+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        assert not app._interpretation_is_stale()


async def test_missing_timestamp_and_no_trades_is_stale(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", None),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T10:00:05+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        assert app.last_tick_monotonic is None
        assert app._interpretation_is_stale()


async def test_price_timestamp_more_than_30s_in_future_is_stale(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:01:00+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T10:00:00+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        assert app.last_tick_monotonic is None
        assert app._interpretation_is_stale()


async def test_price_timestamp_small_future_skew_is_fresh(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(),
        price=Price("AAPL", Decimal("110.00"), "USD", "2026-08-25T10:00:10+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T10:00:00+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        assert app.last_tick_monotonic is not None
        assert not app._interpretation_is_stale()


async def test_snapshot_trades_never_freshen_price_timestamp(tmp_path: Path) -> None:
    snapshot = replace(
        sample_snapshot(),
        trades=(
            Trade(Decimal("108"), Decimal("1"), "2026-08-25T09:00:00+00:00", "USD"),
            Trade(Decimal("110"), Decimal("2"), "2026-08-25T10:00:00+00:00", "USD"),
        ),
        price=Price("AAPL", Decimal("100.00"), "USD", "2026-08-25T08:00:00+00:00"),
    )
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=snapshot,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T10:00:05+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "LIVE"
        assert app.current_timestamp == "2026-08-25T08:00:00+00:00"
        assert app._interpretation_is_stale()


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


async def test_live_picker_starts_without_active_symbol_then_selects_watchlist_row(
    tmp_path: Path,
) -> None:
    app = TossMarketApp(
        None,
        tmp_path / "unused.json",
        connect_live=False,
        settings=Settings(watchlist=("AAPL", "NVDA")),
        manual_live_orders=True,
    )

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert app.symbol == ""
        assert app.connection_state == "SELECT"
        assert app.screen.has_class("symbol-picker")
        assert "관심 종목을 선택" in app.query_one("#summary", Static).render().plain
        table = app.query_one("#watchlist", DataTable)
        assert app.query_one("#watchlist-panel").styles.display != "none"
        symbols = [table.get_cell_at(Coordinate(row, 0)) for row in range(table.row_count)]
        assert symbols == [" AAPL", " NVDA"]

        await pilot.press("b")
        await pilot.pause()
        assert app.last_paper_preview is None
        assert app.screen.has_class("symbol-picker")
        assert any("종목을 선택" in item.message for item in app._notifications)

        await app.action_watch_select()
        await pilot.pause()
        assert app.symbol == "AAPL"
        assert app.market == "us"
        assert not app.screen.has_class("symbol-picker")
        assert table.get_cell_at(Coordinate(0, 0)) == ">AAPL"


async def test_live_picker_add_modal_enter_then_table_enter_selects_symbol(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    store.save(Settings())
    app = TossMarketApp(
        None,
        tmp_path / "unused.json",
        connect_live=False,
        settings_path=settings_path,
        settings=Settings(),
        manual_live_orders=True,
    )

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, WatchlistAddScreen)
        app.screen.query_one("#watchlist-add-input", Input).value = "nvda"

        await pilot.press("enter")
        await pilot.pause()
        table = app.query_one("#watchlist", DataTable)
        assert store.load().watchlist == ("NVDA",)
        assert app.symbol == ""
        assert table.get_cell_at(Coordinate(0, 0)) == " NVDA"

        await pilot.press("enter")
        await pilot.pause()
        assert app.symbol == "NVDA"
        assert table.get_cell_at(Coordinate(0, 0)) == ">NVDA"


async def test_connected_live_picker_loads_watchlist_without_starting_symbol_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    SettingsStore(settings_path).save(Settings(watchlist=("AAPL", "NVDA")))
    calls: list[object] = []

    class FakeCredentials:
        @staticmethod
        def load(path: Path) -> object:
            calls.append(("credentials", path))
            return object()

    class PickerClient:
        def __init__(self, _credentials: object) -> None:
            calls.append("client")

        async def prices(self, symbols: list[str]) -> dict[str, Price]:
            calls.append(("prices", tuple(symbols)))
            return {}

        async def snapshot(self, _symbol: str) -> MarketSnapshot:
            raise AssertionError("snapshot must wait for explicit symbol selection")

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(tui_module, "Credentials", FakeCredentials)
    monkeypatch.setattr(tui_module, "TossMarketClient", PickerClient)
    app = TossMarketApp(
        None,
        tmp_path / "credentials.json",
        settings_path=settings_path,
        manual_live_orders=True,
    )

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert app.symbol == ""
        assert app.watchlist_symbols == ("AAPL", "NVDA")
        assert app.connection_state == "SELECT"
        assert app.feed_task is None
        assert app.candle_sync_task is None
        assert app.watchlist_task is not None
        assert ("prices", ("AAPL", "NVDA")) in calls

    assert "close" in calls


def test_app_binding_keys_are_unique_and_include_watchlist_add() -> None:
    keys = [binding.key for binding in TossMarketApp.BINDINGS]
    assert len(keys) == len(set(keys))
    assert "a" in keys
    assert "question_mark" in keys
    visible = {binding.key for binding in TossMarketApp.BINDINGS if binding.show}
    assert {"q", "r", "a", "1", "c", "i", "question_mark"} <= visible
    assert {"up", "down", "j", "k", "enter"}.isdisjoint(visible)


async def test_help_modal_opens_and_closes_at_compact_size(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        body = app.screen.query_one("#help-body", Static).render().plain
        assert "1분/5분/15분/1시간/일봉" in body
        assert "시장 신호 해석 보기" in body
        assert "관심 목록 이동" in body
        assert app.screen.query_one("#help-dialog").size.height <= 30
        await pilot.press("question_mark")
        await pilot.pause()
        assert len(app.screen.query("#help-dialog")) == 0


async def test_watchlist_input_captures_app_binding_characters(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings_path=tmp_path / "settings.json",
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        symbol_input = app.screen.query_one("#watchlist-add-input", Input)
        await pilot.press("a", "q", "question_mark")
        await pilot.pause()
        assert app.screen.query_one("#watchlist-add-input", Input) is symbol_input
        assert symbol_input.value == "aq?"


async def test_watchlist_add_binding_persists_and_updates_running_table(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    initial = Settings(watchlist=("AAPL",))
    store.save(initial)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings_path=settings_path,
        settings=initial,
    )

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        symbol_input = app.screen.query_one("#watchlist-add-input", Input)
        symbol_input.value = "nvda"
        await pilot.press("enter")
        await pilot.pause()

        assert store.load().watchlist == ("AAPL", "NVDA")
        assert app.settings.watchlist == ("AAPL", "NVDA")
        assert app.watchlist_symbols == ("AAPL", "NVDA")
        table = app.query_one("#watchlist", DataTable)
        assert table.row_count == 2
        assert table.cursor_row == 1

        # Re-adding is idempotent and does not duplicate the persisted row.
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#watchlist-add-input", Input).value = "AAPL"
        await pilot.press("enter")
        await pilot.pause()
        assert store.load().watchlist == ("AAPL", "NVDA")
        assert table.row_count == 2


async def test_watchlist_add_rejects_invalid_symbol_without_writing(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    store = SettingsStore(settings_path)
    initial = Settings(watchlist=("AAPL",))
    store.save(initial)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings_path=settings_path,
        settings=initial,
    )

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#watchlist-add-input", Input).value = "bad symbol!"
        await pilot.press("enter")
        await pilot.pause()

        assert store.load().watchlist == ("AAPL",)
        assert app.watchlist_symbols == ("AAPL",)
        assert app.query_one("#watchlist", DataTable).row_count == 1


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
        orderbook=replace(source.orderbook, currency=currency),
        trades=tuple(replace(trade, currency=currency) for trade in source.trades),
        candles=tuple(replace(candle, currency=currency) for candle in source.candles),
        daily_candles=tuple(replace(candle, currency=currency) for candle in source.daily_candles),
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
        utc_now=_fixed_utc_now("2026-08-25T10:00:03+00:00"),
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


async def test_watchlist_alerts_distinguish_monitored_from_waiting_data(tmp_path: Path) -> None:
    calls = 0
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(
            watchlist=("AAPL", "NVDA"),
            alerts=(
                AlertRule("A1", "AAPL", "above", Decimal("100")),
                AlertRule("A2", "AAPL", "volume-spike", Decimal("2")),
                AlertRule("N1", "NVDA", "above", Decimal("100")),
                AlertRule("N2", "NVDA", "volume-spike", Decimal("2")),
            ),
        ),
    )

    class PriceClient:
        async def prices(self, symbols: list[str]) -> dict[str, Price]:
            nonlocal calls
            calls += 1
            timestamp = app.utc_now().isoformat() if calls == 1 else None
            return {symbol: Price(symbol, Decimal("110"), "USD", timestamp) for symbol in symbols}

        async def close(self) -> None:
            return None

    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = PriceClient()  # type: ignore[assignment]

        assert await app._refresh_watchlist_prices()
        assert app.watchlist_rows["AAPL"].active_alerts == 2
        assert app.watchlist_rows["AAPL"].waiting_alerts == 0
        assert app.watchlist_rows["NVDA"].active_alerts == 1
        assert app.watchlist_rows["NVDA"].waiting_alerts == 1
        table = app.query_one("#watchlist", DataTable)
        assert table.get_cell_at(Coordinate(0, 2)) == "•2"
        assert table.get_cell_at(Coordinate(1, 2)) == "±2"

        assert await app._refresh_watchlist_prices()
        assert app.watchlist_rows["AAPL"].active_alerts == 0
        assert app.watchlist_rows["AAPL"].waiting_alerts == 2
        assert app.watchlist_rows["NVDA"].active_alerts == 0
        assert app.watchlist_rows["NVDA"].waiting_alerts == 2
        assert table.get_cell_at(Coordinate(0, 2)) == "~2"
        assert table.get_cell_at(Coordinate(1, 2)) == "~2"

    assert calls == 2


async def test_operational_transitions_are_allowlisted_bounded_and_visible(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    app._record_operation("ws", "connecting")
    app._record_operation("ws", "connecting")
    assert len(app.operational_transitions) == 1

    app._record_operation("ws", "secret-token-in-state")
    assert len(app.operational_transitions) == 1
    for index in range(205):
        app._record_operation("ws", "connected" if index % 2 else "reconnecting")
    assert len(app.operational_transitions) == 200
    assert all(
        set(transition.__slots__) == {"category", "state", "observed_monotonic"}
        for transition in app.operational_transitions
    )
    assert "secret-token-in-state" not in repr(tuple(app.operational_transitions))

    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        app._render_chrome()
        status = app.query_one("#statusbar", Static).render().plain
        assert "OPS ws:reconnecting" in status


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
        assert "시장 해석" in stats
        assert "차이" in stats
        assert "호가 매도 우세" in stats
        assert "잔량비" in stats
        assert "체결 상승 우세" in stats
        assert "1분량" in stats
        assert "공개 시세 · 관찰 전용" in stats
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


async def test_chart_mode_bindings_switch_title_and_content_without_fetching(
    tmp_path: Path,
) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert app.client is None
        assert app.chart_mode == "1m"
        for key, mode in (("2", "5m"), ("3", "15m"), ("4", "1h"), ("5", "1d"), ("1", "1m")):
            await pilot.press(key)
            await pilot.pause()
            assert app.chart_mode == mode
            # Switching modes must stay local: no client/REST call is ever created.
            assert app.client is None
            title = app.query_one("#chart-panel .panel-title", Static).render().plain
            assert title == f"MARKET CHART · {CHART_MODE_LABELS[mode]}"
            content = app.query_one("#chart-content", Static).render().plain
            assert f"PRICE · {CHART_MODE_LABELS[mode]}" in content


async def test_chart_focus_toggle_hides_watchlist_and_widens_chart(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert not app.screen.has_class("chart-focus")
        assert app.query_one("#watchlist-panel").styles.display != "none"

        await pilot.press("c")
        await pilot.pause()
        assert app.screen.has_class("chart-focus")
        assert app.chart_focus
        assert app.query_one("#watchlist-panel").styles.display == "none"

        chart_width = app.query_one("#chart-panel").size.width
        orderbook_width = app.query_one("#orderbook-panel").size.width
        trades_width = app.query_one("#trades-panel").size.width
        total = chart_width + orderbook_width + trades_width
        assert total > 0
        assert chart_width > orderbook_width > 0
        assert chart_width > trades_width > 0
        ratio = chart_width / total
        assert 0.60 <= ratio <= 0.70
        assert app.query_one("#orderbook", DataTable).max_scroll_x == 0
        assert app.query_one("#trades", DataTable).max_scroll_x == 0

        await pilot.press("c")
        await pilot.pause()
        assert not app.screen.has_class("chart-focus")
        assert not app.chart_focus
        assert app.query_one("#watchlist-panel").styles.display != "none"


async def test_chart_focus_toggle_is_a_no_op_when_compact(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert app.screen.has_class("compact")

        await pilot.press("c")
        await pilot.pause()
        assert not app.screen.has_class("chart-focus")
        assert not app.chart_focus
        assert app.query_one("#chart-panel").styles.display == "none"
        assert app.query_one("#watchlist-panel").styles.display == "none"
        # Chart content must remain safe to render even while fully hidden.
        content = app.query_one("#chart-content", Static).render().plain
        for line in content.splitlines():
            assert cell_len(line) >= 0


async def test_focused_wide_resize_to_compact_exits_focus_cleanly(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.has_class("chart-focus")

        await pilot.resize_terminal(90, 30)
        assert app.screen.has_class("compact")
        assert not app.screen.has_class("chart-focus")
        assert not app.chart_focus
        assert app.query_one("#chart-panel").styles.display == "none"
        assert app.query_one("#watchlist-panel").styles.display == "none"


_NO_ADVICE_WORDS = ("매수 추천", "매도 추천", "BUY", "SELL")


async def test_market_stats_indicator_section_updates_per_timeframe(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        for key, mode in (("2", "5m"), ("3", "15m"), ("4", "1h"), ("5", "1d"), ("1", "1m")):
            await pilot.press(key)
            await pilot.pause()
            stats = app.query_one("#market-stats", Static).render().plain
            assert f"시장 해석 · {TIMEFRAME_LABELS_KO[mode]}" in stats
            assert "EMA9/21" in stats
            assert "RSI" in stats
            assert "거래량" in stats
            assert "VWAP" in stats
            assert "지지" in stats
            assert "저항" in stats
            for word in _NO_ADVICE_WORDS:
                assert word not in stats
        # Daily mode never fabricates a session VWAP.
        await pilot.press("5")
        await pilot.pause()
        assert "VWAP 데이터 부족" in app.query_one("#market-stats", Static).render().plain


async def test_market_stats_fits_wide_and_focused_layouts_without_wrap(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        widget = app.query_one("#market-stats", Static)
        width, height = widget.content_size
        lines = widget.render().plain.splitlines()
        assert len(lines) <= height
        for line in lines:
            assert cell_len(line) <= width

        await pilot.press("c")
        await pilot.pause()
        focused_width, focused_height = widget.content_size
        assert focused_width > width
        focused_lines = widget.render().plain.splitlines()
        assert len(focused_lines) <= focused_height
        for line in focused_lines:
            assert cell_len(line) <= focused_width


async def test_market_stats_stays_safe_when_compact_and_hidden(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        assert app.screen.has_class("compact")
        assert app.query_one("#chart-panel").styles.display == "none"
        content = app.query_one("#market-stats", Static).render().plain
        for line in content.splitlines():
            assert cell_len(line) >= 0


async def test_chart_content_lines_fit_measured_width_wide_and_focused(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        widget = app.query_one("#chart-content", Static)
        width, height = widget.content_size
        assert width > 0
        assert height > 0
        lines = widget.render().plain.splitlines()
        assert len(lines) <= height
        for line in lines:
            assert cell_len(line) <= width

        await pilot.press("c")
        await pilot.pause()
        focused_width, focused_height = widget.content_size
        assert focused_width > width
        focused_lines = widget.render().plain.splitlines()
        assert len(focused_lines) <= focused_height
        for line in focused_lines:
            assert cell_len(line) <= focused_width


# --- indicator error boundary (finding 1) ----------------------------------


def _naive_timestamp_snapshot() -> MarketSnapshot:
    """A snapshot whose newest 1m candle has a naive (offset-less) timestamp.

    ``toss_market_terminal.indicators`` requires timezone-aware candle
    timestamps and raises ``ValueError`` otherwise; this is exactly the
    "malformed/naive candle timestamp" case the review flagged as capable of
    killing the live feed task from inside `_render_stats`.
    """
    base = sample_snapshot()
    bad_candle = replace(base.candles[0], timestamp="2026-08-25T10:00:00")
    return replace(base, candles=(bad_candle, base.candles[1]))


def _currency_mismatch_snapshot() -> MarketSnapshot:
    """A snapshot whose intraday candles and daily candles disagree on currency."""
    base = sample_snapshot()
    mismatched_daily = tuple(replace(candle, currency="KRW") for candle in base.daily_candles)
    return replace(base, daily_candles=mismatched_daily)


async def test_render_stats_survives_malformed_candle_timestamp_at_mount(
    tmp_path: Path,
) -> None:
    broken = _naive_timestamp_snapshot()
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=broken,
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        # `_render_stats` must not propagate the ValueError raised deep inside
        # chart-indicator computation; the app must still be alive and mounted.
        assert app.is_mounted
        assert app.indicator_degraded
        assert app.connection_state == "DEGRADED"
        # Sanitized, bounded detail only -- never the raw offending value.
        assert app.connection_detail == "ValueError: 지표 계산 실패"
        assert "2026-08-25T10:00:00" not in app.connection_detail
        stats = app.query_one("#market-stats", Static).render().plain
        # Base stats (computed from metrics/signals, not chart indicators) still render.
        assert "시장 해석" in stats
        assert "호가" in stats
        assert "지표 계산 실패" in stats
        assert "2026-08-25T10:00:00" not in stats


async def test_indicator_error_in_live_tick_keeps_feed_running_and_recovers_on_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _naive_timestamp_snapshot()
    good = sample_snapshot()
    second_trade_processed = asyncio.Event()

    class RecoveringClient:
        async def snapshot(self, symbol: str) -> MarketSnapshot:
            assert symbol == "AAPL"
            return good

        async def close(self) -> None:
            return None

    class FakeStream:
        def __init__(self, _client: object) -> None:
            pass

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol, Trade(Decimal("111"), Decimal("1"), "2026-08-25T10:00:01Z", "USD")
            )
            yield OrderbookEvent(symbol, good.orderbook)
            yield TradeEvent(
                symbol, Trade(Decimal("112"), Decimal("1"), "2026-08-25T10:00:02Z", "USD")
            )
            second_trade_processed.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("toss_market_terminal.tui.TossMarketStream", FakeStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=broken,
        connect_live=False,
        utc_now=_fixed_utc_now("2026-08-25T10:00:04+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert app.connection_state == "DEGRADED"

        app.client = RecoveringClient()  # type: ignore[assignment]
        app.stream_live = True
        app.subscription_detail = "topics=2"
        app.feed_task = asyncio.create_task(app._run_feed())
        await asyncio.wait_for(second_trade_processed.wait(), timeout=2)
        await pilot.pause()

        # The feed task kept processing ticks through the indicator failure instead of dying.
        assert app.feed_task is not None and not app.feed_task.done()
        assert app.current_price == Decimal("112")
        # No REST snapshot has landed yet: still truthfully degraded, not silently "LIVE".
        assert app.connection_state == "DEGRADED"

        # A subsequent successful REST snapshot (with well-formed candles) restores LIVE.
        assert await app._refresh_snapshot()
        assert app.connection_state == "LIVE"
        assert not app.indicator_degraded
        stats = app.query_one("#market-stats", Static).render().plain
        assert "지표 계산 불가" not in stats


async def test_render_stats_survives_candle_currency_mismatch(tmp_path: Path) -> None:
    broken = _currency_mismatch_snapshot()
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=broken,
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert app.is_mounted
        assert app.indicator_degraded
        assert app.connection_state == "DEGRADED"
        assert app.connection_detail == "ValueError: 지표 계산 실패"
        assert "KRW" not in app.connection_detail
        stats = app.query_one("#market-stats", Static).render().plain
        assert "시장 해석" in stats
        assert "지표 계산 실패" in stats


# --- chart indicator caching (finding 2) ------------------------------------


async def test_chart_indicator_base_is_cached_per_snapshot_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def counting_base(snapshot: MarketSnapshot, mode: str) -> object:
        nonlocal calls
        calls += 1
        return real_chart_indicator_base(snapshot, mode)

    monkeypatch.setattr("toss_market_terminal.tui.chart_indicator_base", counting_base)

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert calls == 1  # on_mount's initial render

        base = real_chart_indicator_base(sample_snapshot(), "1m")
        expected_low = nearest_support_resistance(base.levels, Decimal("109"))
        expected_high = nearest_support_resistance(base.levels, Decimal("111"))
        assert expected_low != expected_high  # sanity: crossing does change the nearest levels

        # Repeated ticks with only the current price moving must hit the cache and still
        # report the correct nearest support/resistance for each price.
        app.current_price = Decimal("109")
        app._render_stats()
        assert app._chart_indicators().levels == expected_low
        assert calls == 1

        app.current_price = Decimal("111")
        app._render_stats()
        assert app._chart_indicators().levels == expected_high
        assert calls == 1

        app._render_stats()
        app._render_stats()
        assert calls == 1  # further repeated renders stay cached

        app._set_chart_mode("5m")
        assert calls == 2  # a mode change invalidates the cache

        app._set_chart_mode("1m")
        assert calls == 3  # switching back is a fresh (snapshot, mode) cache entry

        app._apply_snapshot(sample_snapshot())
        app._render_stats()
        assert calls == 4  # a new snapshot instance invalidates the cache, even with equal content

        app._render_stats()
        app._render_stats()
        assert calls == 4


async def test_switch_symbol_resets_indicator_cache_and_degraded_flag(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("AAPL", "005930")),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app._indicator_base = real_chart_indicator_base(app.snapshot, app.chart_mode)
        app._indicator_base_snapshot = app.snapshot
        app._indicator_base_mode = app.chart_mode
        app.indicator_degraded = True

        await app.switch_symbol("005930")

        assert app._indicator_base is None
        assert app._indicator_base_snapshot is None
        assert app._indicator_base_mode is None
        assert not app.indicator_degraded


# --- unified chart mode labels (finding 3) ----------------------------------


def test_chart_title_labels_duplicate_removed_from_tui_module() -> None:
    assert not hasattr(tui_module, "CHART_TITLE_LABELS")


def test_chart_mode_labels_are_grammatically_consistent() -> None:
    assert CHART_MODE_LABELS == {
        "1m": "1 MINUTE",
        "5m": "5 MINUTES",
        "15m": "15 MINUTES",
        "1h": "1 HOUR",
        "1d": "DAILY",
    }


# --- live candle tracking and REST correction -------------------------------


async def test_trade_event_updates_latest_candle_and_rendered_chart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class OneTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("150"), Decimal("7"), "2026-08-25T10:00:30+09:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", OneTradeStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        chart_render_interval_seconds=0,
        utc_now=_fixed_utc_now("2026-08-25T01:00:32+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        before_chart = app.query_one("#chart-content", Static).render().plain
        app.client = FakeClient()  # type: ignore[assignment]

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        newest = app.snapshot.candles[0]
        assert app.current_price == Decimal("150")
        assert newest.open_price == Decimal("109")
        assert newest.high_price == newest.close_price == Decimal("150")
        assert newest.low_price == Decimal("108")
        assert newest.volume == Decimal("107")
        assert app.query_one("#chart-content", Static).render().plain != before_chart


async def test_high_frequency_trades_are_coalesced_with_a_trailing_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    published = 0

    class FakeClient:
        async def close(self) -> None:
            return None

    class BurstStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            for second, price in enumerate(("120", "121", "122"), start=31):
                yield TradeEvent(
                    symbol,
                    Trade(
                        Decimal(price),
                        Decimal("1"),
                        f"2026-08-25T10:00:{second}+09:00",
                        "USD",
                    ),
                )

    monkeypatch.setattr(tui_module, "TossMarketStream", BurstStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        chart_render_interval_seconds=0.05,
        utc_now=_fixed_utc_now("2026-08-25T01:00:35+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        original_publish = app._publish_live_chart

        def tracked_publish() -> None:
            nonlocal published
            published += 1
            original_publish()

        monkeypatch.setattr(app, "_publish_live_chart", tracked_publish)
        app._last_chart_render_monotonic = tui_module.time.monotonic()
        app.client = FakeClient()  # type: ignore[assignment]

        await app._run_feed(symbol="AAPL", market="us")
        assert app.snapshot.candles[0].close_price == Decimal("110")
        await asyncio.sleep(0.08)
        await pilot.pause()

        assert published == 1
        assert app.snapshot.candles[0].close_price == Decimal("122")
        assert app.snapshot.candles[0].volume == Decimal("103")
        assert app.chart_render_task is None


async def test_candle_resync_replays_trades_received_during_rest_request(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    base = sample_snapshot()

    class RacingCandleClient:
        async def candles(self, symbol: str, *, interval: str = "1m", count: int = 40):
            nonlocal calls
            assert symbol == "AAPL"
            assert count == 200
            calls += 1
            if calls == 2:
                started.set()
            await release.wait()
            return base.daily_candles if interval == "1d" else base.candles

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=base,
        connect_live=False,
        chart_render_interval_seconds=0,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.client = RacingCandleClient()  # type: ignore[assignment]
        refreshing = asyncio.create_task(app._refresh_chart_candles())
        await asyncio.wait_for(started.wait(), timeout=1)

        live_trade = Trade(Decimal("130"), Decimal("4"), "2026-08-25T10:00:40+09:00", "USD")
        app.current_price = live_trade.price
        app.current_timestamp = live_trade.timestamp
        app._record_live_trade(live_trade)
        release.set()

        assert await refreshing
        await pilot.pause()
        assert calls == 2
        assert app.snapshot.candles[0].close_price == Decimal("130")
        assert app.snapshot.candles[0].volume == Decimal("104")


async def test_candle_resync_failure_is_degraded_and_success_recovers(tmp_path: Path) -> None:
    base = sample_snapshot()

    class FailingCandleClient:
        async def candles(self, symbol: str, *, interval: str = "1m", count: int = 40):
            raise RuntimeError(f"secret-candle-error-{symbol}-{interval}-{count}")

        async def close(self) -> None:
            return None

    class SuccessfulCandleClient:
        async def candles(self, symbol: str, *, interval: str = "1m", count: int = 40):
            assert symbol == "AAPL"
            assert count == 200
            return base.daily_candles if interval == "1d" else base.candles

        async def close(self) -> None:
            return None

    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=base,
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.stream_live = True
        app.subscription_detail = "topics=2"
        app.connection_state = "LIVE"
        app.client = FailingCandleClient()  # type: ignore[assignment]

        assert not await app._refresh_chart_candles()
        assert app.candle_sync_degraded
        assert app.connection_state == "DEGRADED"
        assert app.connection_detail == "WS live · candle sync failed"
        assert "secret" not in app.connection_detail

        app.client = SuccessfulCandleClient()  # type: ignore[assignment]
        assert await app._refresh_chart_candles()
        assert not app.candle_sync_degraded
        assert app.connection_state == "LIVE"
        assert app.connection_detail == "topics=2"


async def test_trade_without_snapshot_keeps_tape_quote_and_price_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class OneTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("120"), Decimal("2"), "2026-08-25T10:01:00+09:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", OneTradeStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(
            watchlist=("AAPL",),
            alerts=(AlertRule("A1", "AAPL", "above", Decimal("115")),),
        ),
        utc_now=_fixed_utc_now("2026-08-25T01:01:02+00:00"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.snapshot = None
        app._live_candles = ()
        app._live_daily_candles = ()
        app.current_price = None
        app.current_timestamp = None
        app.client = FakeClient()  # type: ignore[assignment]

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        assert app.current_price == Decimal("120")
        assert app.current_timestamp == "2026-08-25T10:01:00+09:00"
        assert app.trades[0].price == Decimal("120")
        assert app.latest_alert is not None
        assert app.latest_alert.rule.id == "A1"


async def test_late_trade_stays_on_tape_without_rewinding_quote_or_candle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeClient:
        async def close(self) -> None:
            return None

    class LateTradeStream:
        def __init__(self, client: object) -> None:
            _ = client

        async def events(self, symbol: str, market: str):
            _ = market
            yield TradeEvent(
                symbol,
                Trade(Decimal("999"), Decimal("3"), "2026-08-25T09:59:59+09:00", "USD"),
            )

    monkeypatch.setattr(tui_module, "TossMarketStream", LateTradeStream)
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(
            watchlist=("AAPL",),
            alerts=(AlertRule("LATE", "AAPL", "above", Decimal("500")),),
        ),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        original_close = app.snapshot.candles[0].close_price
        original_price = app.current_price
        original_timestamp = app.current_timestamp
        app.client = FakeClient()  # type: ignore[assignment]

        await app._run_feed(symbol="AAPL", market="us")
        await pilot.pause()

        assert app.trades[0].price == Decimal("999")
        assert app.current_price == original_price
        assert app.current_timestamp == original_timestamp
        assert app.snapshot.candles[0].close_price == original_close
        assert app.latest_alert is None


async def test_pending_trailing_render_skips_duplicate_after_immediate_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    published = 0
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        chart_render_interval_seconds=0.05,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        original_publish = app._publish_live_chart

        def tracked_publish() -> None:
            nonlocal published
            published += 1
            original_publish()

        monkeypatch.setattr(app, "_publish_live_chart", tracked_publish)
        app._last_chart_render_monotonic = tui_module.time.monotonic() - 1
        pending = asyncio.create_task(app._publish_live_chart_after(0.03, app.symbol))
        app.chart_render_task = pending

        app._schedule_live_chart_render()
        assert published == 1
        await pending
        await pilot.pause()

        assert published == 1
        assert app.chart_render_task is None
