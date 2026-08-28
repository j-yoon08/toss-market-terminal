"""Display-only local ML direction signal built from validated public candles.

The model in this module is deliberately isolated from account, credential, and
order code.  It is a small k-nearest-neighbour classifier with walk-forward
validation; its output is advisory UI state, never an order intent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from math import isqrt
from typing import Literal

from .models import Candle, MarketSnapshot

MODEL_ID = "local-knn-direction-v1"
POLICY_VERSION = "display-only-v1"
LOOKBACK = 20
MIN_TRAINING_SAMPLES = 48
MIN_VALIDATION_CASES = 12
VALIDATION_WINDOW = 24
MIN_VALIDATION_SCORE = Decimal("0.45")
MIN_DIRECTION_AGREEMENT = Decimal("0.58")
MIN_DIRECTION_MARGIN = Decimal("0.12")
MIN_ADJUSTED_CONFIDENCE = Decimal("0.35")
_DISTANCE_EPSILON = Decimal("0.000001")
_SCALE_EPSILON = Decimal("0.000000000001")

_MODE_CONFIG: dict[str, tuple[int, Decimal, str]] = {
    "1m": (3, Decimal("0.0010"), "약 3분"),
    "5m": (5, Decimal("0.0020"), "약 5분"),
    "15m": (15, Decimal("0.0035"), "약 15분"),
    "1h": (60, Decimal("0.0060"), "약 1시간"),
    "1d": (3, Decimal("0.0100"), "약 3거래일"),
}


class AiDirection(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    INSUFFICIENT = "INSUFFICIENT_DATA"


DataQuality = Literal["validated", "limited", "stale", "invalid"]


@dataclass(frozen=True, slots=True)
class AiDirectionSignal:
    direction: AiDirection
    confidence_percent: int | None
    agreement_percent: int | None
    validation_percent: int | None
    sample_size: int
    neighbor_count: int
    horizon_label: str
    data_quality: DataQuality
    reasons: tuple[str, ...]
    counterpoints: tuple[str, ...]
    risks: tuple[str, ...]
    invalidation: str
    as_of: str | None
    model_id: str = MODEL_ID
    policy_version: str = POLICY_VERSION
    advisory_only: bool = True


@dataclass(frozen=True, slots=True)
class _Example:
    features: tuple[Decimal, ...]
    label: AiDirection


@dataclass(frozen=True, slots=True)
class _Prediction:
    direction: AiDirection
    agreement: Decimal
    margin: Decimal
    votes: tuple[tuple[AiDirection, Decimal], ...]
    neighbor_count: int


def direction_label_ko(direction: AiDirection) -> str:
    return {
        AiDirection.BUY: "매수",
        AiDirection.HOLD: "관망",
        AiDirection.SELL: "매도",
        AiDirection.INSUFFICIENT: "판단 불가",
    }[direction]


def _unavailable(
    reason: str,
    *,
    mode: str,
    quality: DataQuality,
    as_of: str | None = None,
    sample_size: int = 0,
    validation_percent: int | None = None,
) -> AiDirectionSignal:
    horizon = _MODE_CONFIG.get(mode, (0, Decimal("0"), "알 수 없음"))[2]
    return AiDirectionSignal(
        direction=AiDirection.INSUFFICIENT,
        confidence_percent=None,
        agreement_percent=None,
        validation_percent=validation_percent,
        sample_size=sample_size,
        neighbor_count=0,
        horizon_label=horizon,
        data_quality=quality,
        reasons=(reason,),
        counterpoints=(),
        risks=("과거 가격·거래량 패턴만 사용", "뉴스·수수료·슬리피지 미반영"),
        invalidation="검증된 최신 캔들과 충분한 표본이 확보될 때까지 판단 보류",
        as_of=as_of,
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive candle timestamp")
    return parsed


def _validated_chronological(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    items = tuple(reversed(tuple(candles)))
    previous_time: datetime | None = None
    currency: str | None = None
    for candle in items:
        values = (
            candle.open_price,
            candle.high_price,
            candle.low_price,
            candle.close_price,
            candle.volume,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("non-finite candle")
        if min(candle.open_price, candle.high_price, candle.low_price, candle.close_price) <= 0:
            raise ValueError("non-positive candle price")
        if candle.volume < 0:
            raise ValueError("negative candle volume")
        if candle.high_price < candle.low_price or not (
            candle.low_price <= candle.open_price <= candle.high_price
            and candle.low_price <= candle.close_price <= candle.high_price
        ):
            raise ValueError("invalid candle range")
        parsed = _parse_timestamp(candle.timestamp)
        if previous_time is not None and parsed <= previous_time:
            raise ValueError("candles are not strictly newest-first")
        previous_time = parsed
        if currency is None:
            currency = candle.currency
        elif candle.currency != currency:
            raise ValueError("mixed candle currencies")
    return items


def _ratio_change(new: Decimal, old: Decimal) -> Decimal:
    if old <= 0:
        raise ValueError("non-positive denominator")
    return new / old - Decimal("1")


def _features(candles: tuple[Candle, ...], index: int) -> tuple[Decimal, ...]:
    current = candles[index]
    window = candles[index - LOOKBACK : index]
    mean_close = sum((item.close_price for item in window), Decimal("0")) / LOOKBACK
    mean_volume = sum((item.volume for item in window), Decimal("0")) / LOOKBACK
    volume_ratio = current.volume / mean_volume - Decimal("1") if mean_volume > 0 else Decimal("0")
    volume_ratio = max(Decimal("-1"), min(volume_ratio, Decimal("5")))
    return (
        _ratio_change(current.close_price, candles[index - 1].close_price),
        _ratio_change(current.close_price, candles[index - 5].close_price),
        _ratio_change(current.close_price, candles[index - LOOKBACK].close_price),
        _ratio_change(current.close_price, mean_close),
        (current.high_price - current.low_price) / current.close_price,
        volume_ratio,
    )


def _label_for_return(value: Decimal, threshold: Decimal) -> AiDirection:
    if value > threshold:
        return AiDirection.BUY
    if value < -threshold:
        return AiDirection.SELL
    return AiDirection.HOLD


def _examples(
    candles: tuple[Candle, ...], horizon: int, threshold: Decimal
) -> tuple[_Example, ...]:
    result: list[_Example] = []
    last_index = len(candles) - horizon
    for index in range(LOOKBACK, last_index):
        future_return = _ratio_change(
            candles[index + horizon].close_price,
            candles[index].close_price,
        )
        result.append(
            _Example(_features(candles, index), _label_for_return(future_return, threshold))
        )
    return tuple(result)


def _centers_and_scales(
    examples: Sequence[_Example],
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    count = Decimal(len(examples))
    dimensions = len(examples[0].features)
    centers = tuple(
        sum((example.features[index] for example in examples), Decimal("0")) / count
        for index in range(dimensions)
    )
    scales: list[Decimal] = []
    with localcontext() as context:
        context.prec = 34
        for index, center in enumerate(centers):
            variance = (
                sum(
                    ((example.features[index] - center) ** 2 for example in examples),
                    Decimal("0"),
                )
                / count
            )
            scale = variance.sqrt() if variance > _SCALE_EPSILON else Decimal("1")
            scales.append(scale)
    return centers, tuple(scales)


def _predict(examples: Sequence[_Example], current: tuple[Decimal, ...]) -> _Prediction:
    _, scales = _centers_and_scales(examples)
    distances: list[tuple[Decimal, int, _Example]] = []
    for index, example in enumerate(examples):
        distance = sum(
            (
                ((example.features[feature_index] - current[feature_index]) / scales[feature_index])
                ** 2
                for feature_index in range(len(current))
            ),
            Decimal("0"),
        )
        distances.append((distance, index, example))
    distances.sort(key=lambda item: (item[0], item[1]))
    k = max(7, min(15, isqrt(len(examples))))
    if k % 2 == 0:
        k -= 1
    neighbors = distances[:k]
    weights: dict[AiDirection, Decimal] = {
        AiDirection.BUY: Decimal("0"),
        AiDirection.HOLD: Decimal("0"),
        AiDirection.SELL: Decimal("0"),
    }
    for distance, _, example in neighbors:
        weights[example.label] += Decimal("1") / (distance + _DISTANCE_EPSILON)
    total = sum(weights.values(), Decimal("0"))
    votes = tuple((label, weights[label] / total) for label in weights)
    ranked = sorted(votes, key=lambda item: (-item[1], item[0].value))
    top_label, agreement = ranked[0]
    margin = agreement - ranked[1][1]
    if top_label in {AiDirection.BUY, AiDirection.SELL} and (
        agreement < MIN_DIRECTION_AGREEMENT or margin < MIN_DIRECTION_MARGIN
    ):
        top_label = AiDirection.HOLD
    return _Prediction(top_label, agreement, margin, votes, len(neighbors))


def _walk_forward_score(examples: tuple[_Example, ...], horizon: int) -> tuple[Decimal | None, int]:
    start = max(MIN_TRAINING_SAMPLES + horizon, len(examples) - VALIDATION_WINDOW)
    actual_by_class: dict[AiDirection, int] = {}
    correct_by_class: dict[AiDirection, int] = {}
    validation_count = 0
    for index in range(start, len(examples)):
        # An example's label uses the close ``horizon`` bars later.  Embargo the
        # intervening examples so every training label was observable no later
        # than this validation feature timestamp.
        training_end = index - horizon + 1
        prior = examples[:training_end]
        if len(prior) < MIN_TRAINING_SAMPLES or len({item.label for item in prior}) < 2:
            continue
        actual = examples[index].label
        predicted = _predict(prior, examples[index].features).direction
        actual_by_class[actual] = actual_by_class.get(actual, 0) + 1
        if predicted == actual:
            correct_by_class[actual] = correct_by_class.get(actual, 0) + 1
        validation_count += 1
    if validation_count < MIN_VALIDATION_CASES or len(actual_by_class) < 2:
        return None, validation_count
    recalls = tuple(
        Decimal(correct_by_class.get(label, 0)) / count for label, count in actual_by_class.items()
    )
    return sum(recalls, Decimal("0")) / len(recalls), validation_count


def _percent(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1")))


def _vote_share(prediction: _Prediction, label: AiDirection) -> Decimal:
    return dict(prediction.votes)[label]


def _explanation(
    direction: AiDirection,
    features: tuple[Decimal, ...],
    prediction: _Prediction,
    validation_score: Decimal,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    one_return, five_return, long_return, mean_gap, _, volume_ratio = features
    reasons = [
        f"유사 패턴 {prediction.neighbor_count}개 합의 {_percent(prediction.agreement)}%",
        f"walk-forward 균형 정확도 {_percent(validation_score)}%",
    ]
    counterpoints: list[str] = []
    if five_return > 0 and long_return > 0:
        reasons.append("단기·중기 모멘텀 동반 상승")
    elif five_return < 0 and long_return < 0:
        reasons.append("단기·중기 모멘텀 동반 하락")
    elif five_return * long_return < 0:
        counterpoints.append("단기·중기 모멘텀 방향 충돌")
    if mean_gap > 0:
        reasons.append("현재가가 최근 20봉 평균 위")
    elif mean_gap < 0:
        reasons.append("현재가가 최근 20봉 평균 아래")
    if one_return * five_return < 0:
        counterpoints.append("최근 1봉 흐름이 단기 모멘텀과 반대")
    alternative = AiDirection.SELL if direction == AiDirection.BUY else AiDirection.BUY
    alternative_share = _percent(_vote_share(prediction, alternative))
    if alternative_share:
        counterpoints.append(f"반대 방향 유사 패턴 {alternative_share}%")
    if volume_ratio < Decimal("-0.5"):
        counterpoints.append("최근 거래량이 20봉 평균의 절반 미만")
    if direction == AiDirection.BUY:
        invalidation = "현재가가 최근 20봉 평균 아래로 전환되면 매수 판단 무효"
    elif direction == AiDirection.SELL:
        invalidation = "현재가가 최근 20봉 평균 위로 전환되면 매도 판단 무효"
    else:
        invalidation = "유사 패턴의 방향 합의가 기준을 넘기 전까지 관망"
    return tuple(reasons[:4]), tuple(counterpoints[:3]), invalidation


def build_ai_direction_signal(
    snapshot: MarketSnapshot,
    mode: str,
    *,
    stale: bool = False,
) -> AiDirectionSignal:
    """Return a local, display-only direction signal for ``mode``.

    Intraday modes learn from the provider's 1-minute candles and vary only the
    forward horizon. Daily mode learns from daily candles. Every malformed,
    stale, under-sampled, or weakly validated state returns ``INSUFFICIENT_DATA``.
    """

    config = _MODE_CONFIG.get(mode)
    if config is None:
        return _unavailable("지원하지 않는 판단 시간대", mode=mode, quality="invalid")
    horizon, threshold, horizon_label = config
    source = snapshot.daily_candles if mode == "1d" else snapshot.candles
    as_of = source[0].timestamp if source else None
    if stale:
        return _unavailable("시세 신선도 저하", mode=mode, quality="stale", as_of=as_of)
    try:
        candles = _validated_chronological(source)
        minimum_candles = LOOKBACK + horizon + MIN_TRAINING_SAMPLES + MIN_VALIDATION_CASES
        if len(candles) < minimum_candles:
            return _unavailable(
                f"캔들 표본 부족 {len(candles)}/{minimum_candles}",
                mode=mode,
                quality="limited",
                as_of=as_of,
            )
        examples = _examples(candles, horizon, threshold)
        if len(examples) < MIN_TRAINING_SAMPLES + MIN_VALIDATION_CASES:
            return _unavailable(
                "학습 표본 부족",
                mode=mode,
                quality="limited",
                as_of=as_of,
                sample_size=len(examples),
            )
        if len({example.label for example in examples}) < 2:
            return _unavailable(
                "상승·중립·하락 표본 다양성 부족",
                mode=mode,
                quality="limited",
                as_of=as_of,
                sample_size=len(examples),
            )
        validation_score, _ = _walk_forward_score(examples, horizon)
        validation_percent = None if validation_score is None else _percent(validation_score)
        if validation_score is None:
            return _unavailable(
                "walk-forward 검증 표본 부족",
                mode=mode,
                quality="limited",
                as_of=as_of,
                sample_size=len(examples),
            )
        if validation_score < MIN_VALIDATION_SCORE:
            return _unavailable(
                "walk-forward 성능 기준 미달",
                mode=mode,
                quality="limited",
                as_of=as_of,
                sample_size=len(examples),
                validation_percent=validation_percent,
            )
        current_features = _features(candles, len(candles) - 1)
        prediction = _predict(examples, current_features)
        adjusted_confidence = prediction.agreement * validation_score
        direction = prediction.direction
        if direction in {AiDirection.BUY, AiDirection.SELL} and (
            adjusted_confidence < MIN_ADJUSTED_CONFIDENCE
        ):
            direction = AiDirection.HOLD
        reasons, counterpoints, invalidation = _explanation(
            direction, current_features, prediction, validation_score
        )
        quality: DataQuality = "validated" if direction != AiDirection.HOLD else "limited"
        return AiDirectionSignal(
            direction=direction,
            confidence_percent=_percent(adjusted_confidence),
            agreement_percent=_percent(prediction.agreement),
            validation_percent=validation_percent,
            sample_size=len(examples),
            neighbor_count=prediction.neighbor_count,
            horizon_label=horizon_label,
            data_quality=quality,
            reasons=reasons,
            counterpoints=counterpoints,
            risks=("과거 가격·거래량 패턴만 사용", "뉴스·수수료·슬리피지 미반영"),
            invalidation=invalidation,
            as_of=as_of,
        )
    except (ValueError, ArithmeticError, InvalidOperation, OverflowError):
        return _unavailable(
            "캔들 데이터 검증 실패",
            mode=mode,
            quality="invalid",
            as_of=as_of,
        )
