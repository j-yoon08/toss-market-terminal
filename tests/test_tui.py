from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static

from tests.helpers import sample_snapshot
from toss_market_terminal import tui as tui_module
from toss_market_terminal.models import MarketSnapshot, Price, Trade
from toss_market_terminal.render import (
    CHART_MODE_LABELS,
    TIMEFRAME_LABELS_KO,
    nearest_support_resistance,
)
from toss_market_terminal.render import chart_indicator_base as real_chart_indicator_base
from toss_market_terminal.settings import AlertRule, Settings
from toss_market_terminal.stream import OrderbookEvent, StreamStatus, TradeEvent
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
        for key, mode in (("5", "5m"), ("i", "15m"), ("h", "1h"), ("d", "1d"), ("1", "1m")):
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
        for key, mode in (("5", "5m"), ("i", "15m"), ("h", "1h"), ("d", "1d"), ("1", "1m")):
            await pilot.press(key)
            await pilot.pause()
            stats = app.query_one("#market-stats", Static).render().plain
            assert f"{TIMEFRAME_LABELS_KO[mode]} 지표" in stats
            assert "EMA9/21" in stats
            assert "RSI" in stats
            assert "거래량" in stats
            assert "VWAP" in stats
            assert "지지" in stats
            assert "저항" in stats
            for word in _NO_ADVICE_WORDS:
                assert word not in stats
        # Daily mode never fabricates a session VWAP.
        await pilot.press("d")
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
        assert "시장 신호 요약" in stats
        assert "매수·매도 호가 차이" in stats
        assert "지표 계산 불가" in stats
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
        assert "시장 신호 요약" in stats
        assert "지표 계산 불가" in stats


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
