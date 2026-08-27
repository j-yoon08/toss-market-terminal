from __future__ import annotations

from decimal import Decimal

import pytest

from tests.helpers import sample_snapshot
from toss_market_terminal.interpretation import (
    build_market_interpretation,
    interpret_timeframe,
)
from toss_market_terminal.render import (
    ChartIndicators,
    MarketSignals,
    NearestLevel,
    NearestLevels,
)


def indicators(
    *,
    mode: str = "5m",
    ema_short: str | None = "105",
    ema_long: str | None = "100",
    rsi: str | None = "65",
    rsi_previous: str | None = "60",
    volume: str | None = "1.5",
    vwap: str | None = "101",
    support: str | None = "100",
    resistance: str | None = "108",
) -> ChartIndicators:
    def d(value: str | None) -> Decimal | None:
        return Decimal(value) if value is not None else None

    return ChartIndicators(
        mode=mode,
        ema_short=d(ema_short),
        ema_long=d(ema_long),
        rsi=d(rsi),
        rsi_previous=d(rsi_previous),
        relative_volume=d(volume),
        vwap=d(vwap),
        levels=NearestLevels(
            support=(NearestLevel("최근 저가", Decimal(support)) if support is not None else None),
            resistance=(
                NearestLevel("최근 고가", Decimal(resistance)) if resistance is not None else None
            ),
        ),
    )


def signals(book: str | None = "70", tape: str | None = "70") -> MarketSignals:
    return MarketSignals(
        orderbook_imbalance_percent=Decimal(book) if book is not None else None,
        bid_ask_ratio=None,
        vwap_distance_percent=None,
        volume_spike_ratio=None,
        trade_pressure_percent=Decimal(tape) if tape is not None else None,
    )


def test_strong_bullish_agreement_is_evidence_linked() -> None:
    result = interpret_timeframe(indicators(), Decimal("105"), "USD", signals=signals())
    assert result.headline == "상승 우세"
    assert result.confidence == "높음"
    assert "EMA9가 EMA21 위" in result.evidence
    assert "실제 체결 상승 우세" in result.evidence


def test_strong_bearish_agreement() -> None:
    result = interpret_timeframe(
        indicators(
            ema_short="95",
            ema_long="100",
            rsi="35",
            rsi_previous="40",
            vwap="101",
            support="92",
            resistance="100",
        ),
        Decimal("95"),
        "USD",
        signals=signals("30", "30"),
    )
    assert result.headline == "하락 우세"
    assert result.confidence == "높음"
    assert "EMA9가 EMA21 아래" in result.evidence
    assert "실제 체결 하락 우세" in result.evidence


def test_orderbook_and_tape_conflict_is_explicit_and_lowers_confidence() -> None:
    result = interpret_timeframe(indicators(), Decimal("105"), "USD", signals=signals("70", "30"))
    assert "호가와 실제 체결 충돌" in result.risks
    assert result.confidence == "낮음"


def test_bearish_trend_with_price_and_flow_recovery_is_rebound_attempt() -> None:
    result = interpret_timeframe(
        indicators(ema_short="95", ema_long="100", vwap="101"),
        Decimal("105"),
        "USD",
        signals=signals("70", "70"),
    )
    assert result.headline == "반등 시도"


def test_bullish_trend_with_price_and_flow_weakness_is_correction() -> None:
    result = interpret_timeframe(
        indicators(ema_short="105", ema_long="100", vwap="101"),
        Decimal("95"),
        "USD",
        signals=signals("30", "30"),
    )
    assert result.headline == "조정 진행"


def test_insufficient_data_does_not_fabricate_direction_or_levels() -> None:
    result = interpret_timeframe(
        indicators(
            ema_short=None,
            ema_long=None,
            rsi=None,
            rsi_previous=None,
            volume=None,
            vwap=None,
            support=None,
            resistance=None,
        ),
        Decimal("100"),
        "USD",
        signals=signals(None, None),
    )
    assert result.headline == "데이터 부족"
    assert result.confidence == "분석 불가"
    assert result.evidence == ()
    assert result.upside_scenario is None
    assert result.downside_scenario is None


def test_flow_agreement_without_ema_or_vwap_stays_insufficient() -> None:
    result = interpret_timeframe(
        indicators(
            ema_short=None,
            ema_long=None,
            vwap=None,
            rsi="65",
            volume="1.5",
        ),
        Decimal("105"),
        "USD",
        signals=signals("70", "70"),
    )
    assert result.headline == "데이터 부족"
    assert result.confidence == "분석 불가"


@pytest.mark.parametrize(
    ("current_price", "indicator_overrides", "book", "tape"),
    [
        ("0", {}, "70", "70"),
        ("105", {"rsi": "NaN"}, "70", "70"),
        ("105", {"vwap": "Infinity"}, "70", "70"),
        ("105", {"volume": "-1"}, "70", "70"),
        ("105", {}, "101", "70"),
    ],
)
def test_malformed_numeric_inputs_fail_closed(
    current_price: str,
    indicator_overrides: dict[str, str],
    book: str,
    tape: str,
) -> None:
    result = interpret_timeframe(
        indicators(**indicator_overrides),
        Decimal(current_price),
        "USD",
        signals=signals(book, tape),
    )
    assert result.headline == "데이터 부족"
    assert result.confidence == "분석 불가"
    assert result.evidence == ()


@pytest.mark.parametrize(
    ("previous", "expected"),
    [("72", "상승 중"), ("78", "하락 중")],
)
def test_high_rsi_direction_uses_real_previous_value(previous: str, expected: str) -> None:
    result = interpret_timeframe(
        indicators(rsi="75", rsi_previous=previous),
        Decimal("105"),
        "USD",
        signals=signals(),
    )
    assert any(expected in risk for risk in result.risks)


def test_rsi_without_previous_value_does_not_claim_direction() -> None:
    result = interpret_timeframe(
        indicators(rsi="75", rsi_previous=None),
        Decimal("105"),
        "USD",
        signals=signals(),
    )
    rsi_risk = next(risk for risk in result.risks if risk.startswith("RSI"))
    assert "상승 중" not in rsi_risk
    assert "하락 중" not in rsi_risk


def test_support_and_resistance_scenarios_use_real_levels() -> None:
    result = interpret_timeframe(
        indicators(support="100", resistance="106"),
        Decimal("105"),
        "USD",
        signals=signals(),
    )
    assert result.upside_scenario is not None and "106.00" in result.upside_scenario
    assert result.downside_scenario is not None and "100.00" in result.downside_scenario
    assert any("저항까지" in risk for risk in result.risks)


def test_multi_timeframe_disagreement_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    bullish = {
        mode: indicators(mode=mode, vwap=None if mode == "1d" else "101")
        for mode in ("1m", "5m", "15m")
    }
    bearish_daily = indicators(
        mode="1d",
        ema_short="100",
        ema_long="110",
        vwap=None,
        volume="1.5",
    )

    def fake_chart_indicators(_snapshot, mode: str, _price: Decimal) -> ChartIndicators:
        if mode == "1d":
            return bearish_daily
        return bullish[mode]

    monkeypatch.setattr(
        "toss_market_terminal.interpretation.chart_indicators",
        fake_chart_indicators,
    )
    result = build_market_interpretation(
        sample_snapshot(),
        Decimal("105"),
        "USD",
        "5m",
        signals(),
        selected_indicators=bullish["5m"],
    )
    assert "일봉은 하락" in result.alignment
    assert len(result.timeframes) == 4


def test_multi_timeframe_alignment_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = indicators(mode="5m")

    def fake_chart_indicators(_snapshot, mode: str, _price: Decimal) -> ChartIndicators:
        return indicators(mode=mode, vwap=None if mode == "1d" else "101")

    monkeypatch.setattr(
        "toss_market_terminal.interpretation.chart_indicators",
        fake_chart_indicators,
    )
    result = build_market_interpretation(
        sample_snapshot(),
        Decimal("105"),
        "USD",
        "5m",
        signals(),
        selected_indicators=selected,
    )
    assert "상승 방향으로 정렬" in result.alignment
    assert all(item.headline == "상승 우세" for item in result.timeframes)


def test_one_malformed_timeframe_does_not_break_full_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = indicators(mode="5m")

    def fake_chart_indicators(_snapshot, mode: str, _price: Decimal) -> ChartIndicators:
        if mode == "15m":
            raise ValueError("malformed")
        return indicators(mode=mode, vwap=None if mode == "1d" else "101")

    monkeypatch.setattr(
        "toss_market_terminal.interpretation.chart_indicators",
        fake_chart_indicators,
    )
    result = build_market_interpretation(
        sample_snapshot(),
        Decimal("105"),
        "USD",
        "5m",
        signals(),
        selected_indicators=selected,
    )
    broken = next(item for item in result.timeframes if item.mode == "15m")
    assert broken.headline == "데이터 부족"
    assert broken.confidence == "분석 불가"
    assert result.selected.mode == "5m"


def test_stale_build_skips_indicator_calculation_and_marks_every_timeframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_chart_indicators(*_args: object, **_kwargs: object) -> ChartIndicators:
        raise AssertionError("stale interpretation must not calculate indicators")

    monkeypatch.setattr(
        "toss_market_terminal.interpretation.chart_indicators",
        forbidden_chart_indicators,
    )
    result = build_market_interpretation(
        sample_snapshot(),
        Decimal("105"),
        "USD",
        "5m",
        signals(),
        stale=True,
    )
    assert result.selected.data_quality == "stale"
    assert result.selected.headline == "데이터 부족"
    assert result.selected.confidence == "분석 불가"
    assert result.selected.risks == ("시세 신선도 저하",)
    assert all(item.data_quality == "stale" for item in result.timeframes)
    assert "신선도가 낮아" in result.alignment
