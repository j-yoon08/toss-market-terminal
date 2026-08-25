from __future__ import annotations

from pathlib import Path

from textual.widgets import Static

from tests.helpers import sample_snapshot
from toss_market_terminal.models import MarketSnapshot
from toss_market_terminal.stream import StreamStatus
from toss_market_terminal.tui import TossMarketApp


async def test_wide_tui_renders_three_panel_market_console(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        assert "TOSS MARKET" in app.query_one("#topbar", Static).render().plain
        assert app.query_one("#orderbook-panel .panel-title", Static).render().plain == "ORDER BOOK"
        assert "MARKET CHART" in app.query_one("#chart-panel .panel-title", Static).render().plain
        assert app.query_one("#trades-panel .panel-title", Static).render().plain == "LIVE TRADES"
        assert "RECENT VWAP" in app.query_one("#market-stats", Static).render().plain
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
