from __future__ import annotations

from datetime import UTC, datetime

import pytest

from toss_market_terminal.demo import build_demo_app, demo_snapshot


def test_demo_snapshot_is_valid_recent_and_bounded() -> None:
    now = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)
    snapshot = demo_snapshot(now=now)
    assert snapshot.stock.symbol == "AAPL"
    assert snapshot.price.timestamp == now.isoformat()
    assert 180 <= len(snapshot.candles) <= 240
    assert 30 <= len(snapshot.daily_candles) <= 90
    assert snapshot.candles[0].timestamp > snapshot.candles[-1].timestamp


@pytest.mark.asyncio
async def test_demo_app_is_offline_paper_preview() -> None:
    app = build_demo_app(now=datetime(2026, 8, 30, 6, 0, tzinfo=UTC))
    assert app.connect_live is False
    assert app.manual_live_orders is False
    assert app.offline_demo is True
    assert app.client is None
    assert app.initial_snapshot is not None
    assert app.SUB_TITLE == "DEMO · OFFLINE · PAPER ONLY"
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.connection_state == "PREVIEW"
        assert app.client is None
        assert "DEMO · OFFLINE · PAPER ONLY" in app.query_one("#topbar").render().plain
        assert "OFFLINE DEMO" in app.query_one("#statusbar").render().plain
