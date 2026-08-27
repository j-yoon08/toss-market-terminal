from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .models import MarketSnapshot
from .render import ChartIndicators, MarketSignals, chart_indicators

Headline = Literal[
    "상승 우세",
    "하락 우세",
    "반등 시도",
    "조정 진행",
    "박스권",
    "신호 충돌",
    "데이터 부족",
]
Confidence = Literal["높음", "보통", "낮음", "분석 불가"]
DataQuality = Literal["fresh", "stale", "insufficient"]

TIMEFRAME_LABELS: dict[str, str] = {
    "1m": "1분",
    "5m": "5분",
    "15m": "15분",
    "1h": "1시간",
    "1d": "일봉",
}
MULTI_TIMEFRAMES = ("1m", "5m", "15m", "1d")

# Canonical signal boundaries already used by render.py. Values exactly at
# 40/60 remain neutral rather than being forced into a directional reading.
FLOW_LOW = Decimal("40")
FLOW_HIGH = Decimal("60")
VOLUME_CONFIRM = Decimal("1.3")
VOLUME_WEAK = Decimal("0.7")
NEAR_LEVEL_PERCENT = Decimal("1.5")


@dataclass(frozen=True, slots=True)
class TimeframeInterpretation:
    mode: str
    label: str
    headline: Headline
    confidence: Confidence
    evidence: tuple[str, ...]
    risks: tuple[str, ...]
    upside_scenario: str | None
    downside_scenario: str | None
    data_quality: DataQuality


@dataclass(frozen=True, slots=True)
class MarketInterpretation:
    selected: TimeframeInterpretation
    timeframes: tuple[TimeframeInterpretation, ...]
    alignment: str


def _direction(value: Decimal | None) -> int | None:
    if value is None:
        return None
    if value > FLOW_HIGH:
        return 1
    if value < FLOW_LOW:
        return -1
    return 0


def _format_price(value: Decimal, currency: str) -> str:
    if currency.upper() == "KRW":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _format_ratio(value: Decimal) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _level_distance_percent(level: Decimal, current_price: Decimal) -> Decimal | None:
    if current_price <= 0:
        return None
    return abs(level - current_price) / current_price * Decimal("100")


def _valid_decimal(value: Decimal | None, *, positive: bool = False) -> bool:
    if value is None:
        return True
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    return not positive or value > 0


def _inputs_are_valid(
    indicators: ChartIndicators,
    current_price: Decimal,
    signals: MarketSignals | None,
) -> bool:
    if not _valid_decimal(current_price, positive=True):
        return False
    for value in (
        indicators.ema_short,
        indicators.ema_long,
        indicators.vwap,
        indicators.levels.support.price if indicators.levels.support is not None else None,
        indicators.levels.resistance.price if indicators.levels.resistance is not None else None,
    ):
        if not _valid_decimal(value, positive=True):
            return False
    for value in (indicators.rsi, indicators.rsi_previous):
        if not _valid_decimal(value) or (
            value is not None and not Decimal("0") <= value <= Decimal("100")
        ):
            return False
    if not _valid_decimal(indicators.relative_volume) or (
        indicators.relative_volume is not None and indicators.relative_volume < 0
    ):
        return False
    if signals is not None:
        for value in (
            signals.orderbook_imbalance_percent,
            signals.trade_pressure_percent,
        ):
            if not _valid_decimal(value) or (
                value is not None and not Decimal("0") <= value <= Decimal("100")
            ):
                return False
    return True


def _headline_sentence(headline: Headline) -> str:
    return {
        "상승 우세": "추세와 현재 위치가 단기 상승 흐름을 가리킵니다.",
        "하락 우세": "추세와 현재 위치가 단기 하락 흐름을 가리킵니다.",
        "반등 시도": "기존 약세 흐름 안에서 가격과 체결이 회복을 시도합니다.",
        "조정 진행": "기존 강세 흐름 안에서 가격과 체결이 약해지고 있습니다.",
        "박스권": "방향 신호가 중립에 가까워 범위 안 움직임으로 해석됩니다.",
        "신호 충돌": "추세·호가·실제 체결이 같은 방향으로 정렬되지 않았습니다.",
        "데이터 부족": "현재 데이터만으로 방향을 설명하기 어렵습니다.",
    }[headline]


def interpretation_explanation(item: TimeframeInterpretation) -> str:
    """Bounded Korean prose whose claims are traceable to returned evidence."""
    parts = [_headline_sentence(item.headline)]
    if item.evidence:
        parts.append("근거는 " + " · ".join(item.evidence) + "입니다.")
    if item.risks:
        parts.append("반대 신호는 " + " · ".join(item.risks) + "입니다.")
    return " ".join(parts)


def interpret_timeframe(
    indicators: ChartIndicators,
    current_price: Decimal,
    currency: str,
    *,
    signals: MarketSignals | None = None,
    stale: bool = False,
) -> TimeframeInterpretation:
    """Explain one timeframe without making an execution recommendation."""
    if stale:
        return stale_timeframe(indicators.mode)
    if not _inputs_are_valid(indicators, current_price, signals):
        return unavailable_timeframe(indicators.mode)
    evidence: list[str] = []
    risks: list[str] = []
    votes: dict[str, int] = {}

    if indicators.ema_short is not None and indicators.ema_long is not None:
        if indicators.ema_short > indicators.ema_long:
            votes["ema"] = 1
            evidence.append("EMA9가 EMA21 위")
        elif indicators.ema_short < indicators.ema_long:
            votes["ema"] = -1
            evidence.append("EMA9가 EMA21 아래")
        else:
            votes["ema"] = 0

        if current_price > indicators.ema_long:
            votes["ema_location"] = 1
            evidence.append("현재가가 EMA21 위")
        elif current_price < indicators.ema_long:
            votes["ema_location"] = -1
            evidence.append("현재가가 EMA21 아래")
        else:
            votes["ema_location"] = 0

    if indicators.vwap is not None and indicators.vwap > 0:
        distance = (current_price - indicators.vwap) / indicators.vwap * Decimal("100")
        if distance > 0:
            votes["vwap"] = 1
            evidence.append(f"VWAP 위 {_format_ratio(abs(distance))}%")
        elif distance < 0:
            votes["vwap"] = -1
            evidence.append(f"VWAP 아래 {_format_ratio(abs(distance))}%")
        else:
            votes["vwap"] = 0

    book_direction: int | None = None
    tape_direction: int | None = None
    if signals is not None:
        book_direction = _direction(signals.orderbook_imbalance_percent)
        tape_direction = _direction(signals.trade_pressure_percent)
        if book_direction is not None:
            votes["book"] = book_direction
            if book_direction > 0:
                evidence.append("호가 매수 잔량 우세")
            elif book_direction < 0:
                evidence.append("호가 매도 잔량 우세")
        if tape_direction is not None:
            votes["tape"] = tape_direction
            if tape_direction > 0:
                evidence.append("실제 체결 상승 우세")
            elif tape_direction < 0:
                evidence.append("실제 체결 하락 우세")

    book_tape_conflict = (
        book_direction not in (None, 0)
        and tape_direction not in (None, 0)
        and book_direction != tape_direction
    )
    if book_tape_conflict:
        risks.append("호가와 실제 체결 충돌")

    ema_direction = votes.get("ema")
    flow_direction = tape_direction if tape_direction not in (None, 0) else book_direction
    trend_flow_conflict = (
        ema_direction not in (None, 0)
        and flow_direction not in (None, 0)
        and ema_direction != flow_direction
    )
    if trend_flow_conflict:
        risks.append("추세와 단기 수급 방향 불일치")

    if indicators.rsi is not None:
        rsi_text = _format_ratio(indicators.rsi)
        previous = indicators.rsi_previous
        movement = ""
        if previous is not None:
            if indicators.rsi > previous:
                movement = " · 상승 중"
            elif indicators.rsi < previous:
                movement = " · 하락 중"
        if indicators.rsi > Decimal("70"):
            risks.append(f"RSI {rsi_text} 높은 구간{movement}")
        elif indicators.rsi < Decimal("30"):
            risks.append(f"RSI {rsi_text} 낮은 구간{movement}")

    volume_confirmed = False
    if indicators.relative_volume is not None:
        if indicators.relative_volume >= VOLUME_CONFIRM:
            volume_confirmed = True
            evidence.append(f"거래량 {_format_ratio(indicators.relative_volume)}배")
        elif indicators.relative_volume < VOLUME_WEAK:
            risks.append(f"거래량 {_format_ratio(indicators.relative_volume)}배로 확인 약함")

    support = indicators.levels.support
    resistance = indicators.levels.resistance
    upside_scenario = None
    downside_scenario = None
    if resistance is not None:
        price = _format_price(resistance.price, currency)
        upside_scenario = f"저항 {price} 돌파·거래량 유지 시 상승 강화"
        distance = _level_distance_percent(resistance.price, current_price)
        if distance is not None and distance <= NEAR_LEVEL_PERCENT:
            risks.append(f"저항까지 {_format_ratio(distance)}%")
    if support is not None:
        price = _format_price(support.price, currency)
        downside_scenario = f"지지 {price} 이탈 시 현재 해석 약화"

    directional = [value for value in votes.values() if value != 0]
    positive = directional.count(1)
    negative = directional.count(-1)
    available = len(votes)
    has_price_context = "ema" in votes or "vwap" in votes

    if not has_price_context:
        headline: Headline = "데이터 부족"
        confidence: Confidence = "분석 불가"
    elif len(directional) < 2:
        if available >= 2 and not directional:
            headline = "박스권"
            confidence = "낮음"
        else:
            headline = "데이터 부족"
            confidence = "분석 불가"
    else:
        recovering = ema_direction == -1 and votes.get("vwap") == 1 and flow_direction == 1
        correcting = ema_direction == 1 and votes.get("vwap") == -1 and flow_direction == -1
        if recovering:
            headline = "반등 시도"
        elif correcting:
            headline = "조정 진행"
        elif positive >= negative + 2:
            headline = "상승 우세"
        elif negative >= positive + 2:
            headline = "하락 우세"
        elif book_tape_conflict or trend_flow_conflict or (positive and negative):
            headline = "신호 충돌"
        else:
            headline = "박스권"

        winner = max(positive, negative)
        loser = min(positive, negative)
        if headline == "신호 충돌" or book_tape_conflict:
            confidence = "낮음"
        elif winner >= 3 and loser == 0 and volume_confirmed:
            confidence = "높음"
        elif winner >= 2 and winner > loser:
            confidence = "보통"
        else:
            confidence = "낮음"

    return TimeframeInterpretation(
        mode=indicators.mode,
        label=TIMEFRAME_LABELS.get(indicators.mode, indicators.mode),
        headline=headline,
        confidence=confidence,
        evidence=tuple(evidence[:5]),
        risks=tuple(risks[:4]),
        upside_scenario=upside_scenario,
        downside_scenario=downside_scenario,
        data_quality="insufficient" if headline == "데이터 부족" else "fresh",
    )


def unavailable_timeframe(mode: str) -> TimeframeInterpretation:
    return TimeframeInterpretation(
        mode=mode,
        label=TIMEFRAME_LABELS.get(mode, mode),
        headline="데이터 부족",
        confidence="분석 불가",
        evidence=(),
        risks=("지표 계산 불가",),
        upside_scenario=None,
        downside_scenario=None,
        data_quality="insufficient",
    )


def stale_timeframe(mode: str) -> TimeframeInterpretation:
    return TimeframeInterpretation(
        mode=mode,
        label=TIMEFRAME_LABELS.get(mode, mode),
        headline="데이터 부족",
        confidence="분석 불가",
        evidence=(),
        risks=("시세 신선도 저하",),
        upside_scenario=None,
        downside_scenario=None,
        data_quality="stale",
    )


def _alignment(items: tuple[TimeframeInterpretation, ...]) -> str:
    direction = {
        "상승 우세": 1,
        "반등 시도": 1,
        "하락 우세": -1,
        "조정 진행": -1,
    }
    available = [(item, direction[item.headline]) for item in items if item.headline in direction]
    if len(available) < 2:
        return "시간대 정렬을 판단할 데이터가 부족합니다."

    values = [value for _, value in available]
    labels = "·".join(item.label for item, _ in available)
    if all(value == 1 for value in values):
        return f"{labels} 흐름이 상승 방향으로 정렬돼 있습니다."
    if all(value == -1 for value in values):
        return f"{labels} 흐름이 하락 방향으로 정렬돼 있습니다."

    daily = next((value for item, value in available if item.mode == "1d"), None)
    intraday = [value for item, value in available if item.mode != "1d"]
    if daily == -1 and intraday.count(1) >= 2:
        return "분봉은 회복 중이지만 일봉은 하락 방향이라 큰 흐름 안의 단기 반등으로 관찰됩니다."
    if daily == 1 and intraday.count(-1) >= 2:
        return "분봉은 약해졌지만 일봉은 상승 방향이라 큰 흐름 안의 단기 조정으로 관찰됩니다."
    return "시간대별 방향이 섞여 있어 한 방향으로 정렬되지 않았습니다."


def build_market_interpretation(
    snapshot: MarketSnapshot,
    current_price: Decimal,
    currency: str,
    selected_mode: str,
    signals: MarketSignals,
    *,
    selected_indicators: ChartIndicators | None = None,
    stale: bool = False,
) -> MarketInterpretation:
    if stale:
        selected = stale_timeframe(selected_mode)
        items = tuple(stale_timeframe(mode) for mode in MULTI_TIMEFRAMES)
        return MarketInterpretation(
            selected=selected,
            timeframes=items,
            alignment="시세 신선도가 낮아 시간대 정렬을 판단하지 않습니다.",
        )
    selected_data = selected_indicators or chart_indicators(snapshot, selected_mode, current_price)
    selected = interpret_timeframe(
        selected_data,
        current_price,
        currency,
        signals=signals,
    )

    timeframes: list[TimeframeInterpretation] = []
    for mode in MULTI_TIMEFRAMES:
        try:
            indicators = (
                selected_data
                if mode == selected_mode
                else chart_indicators(snapshot, mode, current_price)
            )
            timeframe_signals = signals if mode == selected_mode else None
            timeframes.append(
                interpret_timeframe(
                    indicators,
                    current_price,
                    currency,
                    signals=timeframe_signals,
                )
            )
        except (ValueError, ArithmeticError):
            timeframes.append(unavailable_timeframe(mode))

    items = tuple(timeframes)
    return MarketInterpretation(selected=selected, timeframes=items, alignment=_alignment(items))
