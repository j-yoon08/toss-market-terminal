from __future__ import annotations

from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import Static

from tests.helpers import sample_snapshot as _sample_snapshot
from toss_market_terminal.settings import Settings
from toss_market_terminal.tui import TossMarketApp


def sample_snapshot():
    return _sample_snapshot(fresh_price=True)


FORBIDDEN = ("매수 추천", "매도 추천", "BUY", "SELL", "무조건", "확실", "매수 적기")


@pytest.mark.parametrize("size", [(140, 42), (90, 30)])
async def test_interpretation_modal_opens_and_closes_at_wide_and_compact_sizes(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("AAPL",)),
    )
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        dialog = app.screen.query_one("#interpretation-dialog")
        body = app.screen.query_one("#interpretation-body", Static).render().plain
        assert dialog.size.width <= size[0]
        assert dialog.size.height <= size[1]
        assert "시간대별 흐름" in body
        assert "열기 시점의 공개 시세 기반" in body
        assert "1분" in body and "5분" in body and "15분" in body and "일봉" in body
        for word in FORBIDDEN:
            assert word not in body
        await pilot.press("i")
        await pilot.pause()
        assert len(app.screen.query("#interpretation-dialog")) == 0


async def test_interpretation_modal_uses_selected_timeframe_and_isolates_app_bindings(
    tmp_path: Path,
) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
        settings=Settings(watchlist=("AAPL", "NVDA")),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert app.chart_mode == "5m"
        symbol_before = app.symbol
        await pilot.press("i")
        await pilot.pause()
        body = app.screen.query_one("#interpretation-body", Static).render().plain
        assert body.startswith("5분 ·")

        # These keys are app actions outside the modal. They must remain inert
        # while the explanation is open.
        await pilot.press("1", "c", "p", "b", "s", "r", "a", "q", "j", "k", "enter", "down", "up")
        await pilot.pause()
        assert app.chart_mode == "5m"
        assert app.symbol == symbol_before
        assert app.is_running
        assert len(app.screen.query("#interpretation-dialog")) == 1
        assert len(app.screen.query("#order-ticket-dialog")) == 0

        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen.query("#interpretation-dialog")) == 0


async def test_interpretation_open_is_in_memory_only_and_summary_is_cell_bounded(
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
        stats = app.query_one("#market-stats", Static)
        lines = stats.render().plain.splitlines()
        assert len(lines) == 9
        assert all(cell_len(line) <= 52 for line in lines)
        assert lines[0].startswith("시장 해석 · 1분봉")
        assert "근거 ·" in lines[1]
        assert "주의 ·" in lines[2]
        assert "조건 ·" in lines[3]
        assert not lines[3].endswith("…")
        assert not lines[7].endswith("…")
        for word in FORBIDDEN:
            assert word not in stats.render().plain

        await pilot.press("i")
        await pilot.pause()
        assert app.client is None
        assert app.chart_mode == "1m"


async def test_indicator_arithmetic_error_fails_closed_in_summary_and_modal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()

        def broken_indicators() -> None:
            raise ArithmeticError("sensitive raw value")

        monkeypatch.setattr(app, "_chart_indicators", broken_indicators)
        app._render_stats()
        stats = app.query_one("#market-stats", Static).render().plain
        assert "데이터 부족" in stats
        assert "ArithmeticError: 지표 계산 실패" in stats
        assert "sensitive raw value" not in stats

        await pilot.press("i")
        await pilot.pause()
        body = app.screen.query_one("#interpretation-body", Static).render().plain
        assert "데이터 상태 · 신선도 저하" in body
        assert "sensitive raw value" not in body


async def test_degraded_market_data_is_not_presented_as_fresh(tmp_path: Path) -> None:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=sample_snapshot(),
        connect_live=False,
    )
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.connection_state = "DEGRADED"
        app.candle_sync_degraded = True
        app._render_stats()
        stats = app.query_one("#market-stats", Static).render().plain
        assert stats.splitlines()[0].startswith("시장 해석 · 1분봉 데이터 부족")
        assert "시세 신선도 저하" in stats
        assert "신뢰도 분석 불가" in stats

        await pilot.press("i")
        await pilot.pause()
        body = app.screen.query_one("#interpretation-body", Static).render().plain
        assert "데이터 상태 · 신선도 저하" in body
        assert "시세 신선도 저하" in body
        assert "상승 우세 · 신뢰도" not in body
