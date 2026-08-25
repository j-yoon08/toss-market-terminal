"""Pure chart calculation helpers: timeframe aggregation, momentum, price levels."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from toss_market_terminal.models import Candle

SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "1h")
_BUCKET_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
_MAX_ECHO = 32
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_THREE = Decimal("3")
_FIFTY = Decimal("50")
_HUNDRED = Decimal("100")


def _echo(value: object) -> str:
    """Bounded echo of an offending value for error messages."""
    text = str(value)
    if len(text) > _MAX_ECHO:
        text = text[:_MAX_ECHO] + "…"
    return text


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("캔들 타임스탬프는 비어 있지 않은 문자열이어야 합니다.")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"캔들 타임스탬프를 해석할 수 없습니다: {_echo(value)}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"캔들 타임스탬프에 시간대 정보가 없습니다: {_echo(value)}")
    return parsed


def _bucket_start(parsed: datetime, bucket_minutes: int) -> datetime:
    """Floor ``parsed`` to the ``bucket_minutes`` wall-clock boundary, keeping its zone."""
    return parsed.replace(
        minute=parsed.minute - parsed.minute % bucket_minutes, second=0, microsecond=0
    )


def _bucket_key(start: datetime) -> tuple[int, int, int]:
    # Local date plus wall-clock minute-of-day plus the zone's UTC offset keeps
    # candles that share a local date and wall-clock bucket but differ by UTC
    # offset (e.g. 09:04+09:00 vs 09:04Z) in separate buckets.
    return start.date().toordinal(), start.hour * 60 + start.minute, _offset_minutes(start)


def _offset_minutes(parsed: datetime) -> int:
    offset = parsed.utcoffset()
    if offset is None:  # defensive; callers already require an aware datetime
        raise ValueError("캔들 타임스탬프에 시간대 정보가 없습니다.")
    return int(offset.total_seconds()) // 60


def aggregate_candles(candles: Sequence[Candle], timeframe: str) -> tuple[Candle, ...]:
    """Aggregate newest-first 1m ``candles`` into ``timeframe`` candles (newest-first).

    Supported timeframes: ``1m`` (identity), ``5m``, ``15m``, and ``1h``. Buckets
    follow the local wall clock (local date + boundary minute + UTC offset) and
    never cross dates; an incomplete latest bucket is included. OHLC uses the
    earliest open, latest close, highest high, lowest low; volume is summed. All
    currencies must match. Raises ``ValueError`` for malformed or naive
    timestamps, currency mismatch, or an unsupported timeframe.
    """
    bucket_minutes = _BUCKET_MINUTES.get(timeframe)
    if bucket_minutes is None:
        raise ValueError(
            f"지원하지 않는 타임프레임입니다: {_echo(timeframe)} (지원: 1m, 5m, 15m, 1h)"
        )
    items = tuple(candles)
    if timeframe == "1m":
        return items

    buckets: dict[tuple[int, int, int], list[tuple[datetime, datetime, Candle]]] = {}
    currency: str | None = None
    for candle in items:
        if currency is None:
            currency = candle.currency
        elif candle.currency != currency:
            raise ValueError(
                f"캔들 통화가 일치하지 않습니다: {_echo(currency)} != {_echo(candle.currency)}"
            )
        parsed = _parse_timestamp(candle.timestamp)
        start = _bucket_start(parsed, bucket_minutes)
        buckets.setdefault(_bucket_key(start), []).append((start, parsed, candle))

    aggregated: list[Candle] = []
    for members in buckets.values():  # insertion order follows newest-first input
        members.sort(key=lambda item: item[1])  # chronological order within the bucket
        start = members[0][0]
        oldest = members[0][2]
        latest = members[-1][2]
        aggregated.append(
            Candle(
                timestamp=start.isoformat(),
                open_price=oldest.open_price,
                high_price=max(candle.high_price for _, _, candle in members),
                low_price=min(candle.low_price for _, _, candle in members),
                close_price=latest.close_price,
                volume=sum((candle.volume for _, _, candle in members), _ZERO),
                currency=str(currency),
            )
        )
    return tuple(aggregated)


def _validate_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 값은 정수여야 합니다: {_echo(value)}")
    if value <= 0:
        raise ValueError(f"{label} 값은 양수여야 합니다: {value}")
    return value


def _require_single_currency(candles: tuple[Candle, ...]) -> str | None:
    currency: str | None = None
    for candle in candles:
        if currency is None:
            currency = candle.currency
        elif candle.currency != currency:
            raise ValueError(
                f"캔들 통화가 일치하지 않습니다: {_echo(currency)} != {_echo(candle.currency)}"
            )
    return currency


def _session_key(parsed: datetime) -> tuple[int, int]:
    """Session identity: local calendar date plus the zone's UTC offset."""
    return parsed.date().toordinal(), _offset_minutes(parsed)


def ema_series(candles: Sequence[Candle], period: int) -> tuple[Decimal | None, ...]:
    """Exponential moving average series aligned with newest-first ``candles``.

    Positions before ``period`` closes are ``None``. Seeding is a chronological
    simple average of the first ``period`` closes; afterwards each value is
    ``close * alpha + previous * (1 - alpha)`` with Decimal ``alpha = 2 /
    (period + 1)``. All currencies must match. Raises ``ValueError`` when
    ``period`` is not an integer (bools rejected), is zero/negative, or on a
    currency mismatch.
    """
    checked_period = _validate_positive_int(period, "EMA 기간")
    items = tuple(candles)
    chronological = tuple(reversed(items))
    _require_single_currency(chronological)
    count = len(chronological)
    alpha = _TWO / Decimal(checked_period + 1)
    complement = _ONE - alpha
    result: list[Decimal | None] = [None] * count
    if count >= checked_period:
        seed = sum((c.close_price for c in chronological[:checked_period]), _ZERO) / checked_period
        previous = seed
        result[checked_period - 1] = seed
        for index in range(checked_period, count):
            close = chronological[index].close_price
            current = close * alpha + previous * complement
            result[index] = current
            previous = current
    return tuple(reversed(result))


def session_vwap_series(candles: Sequence[Candle]) -> tuple[Decimal | None, ...]:
    """Session VWAP series aligned with newest-first ``candles``.

    Walks candles chronologically, accumulating typical price ``(high + low +
    close) / 3`` weighted by volume per local date and UTC offset (session
    identity); the accumulator resets whenever that identity changes. Positions
    before any positive cumulative volume are ``None``. All currencies must
    match. Raises ``ValueError`` on a currency mismatch.
    """
    items = tuple(candles)
    chronological = tuple(reversed(items))
    _require_single_currency(chronological)
    cumulative_pv = _ZERO
    cumulative_volume = _ZERO
    session_key: tuple[int, int] | None = None
    results: list[Decimal | None] = [None] * len(items)
    for index in range(len(items) - 1, -1, -1):  # chronological walk over newest-first input
        candle = items[index]
        parsed = _parse_timestamp(candle.timestamp)
        key = _session_key(parsed)
        if session_key is None:
            session_key = key
        elif key != session_key:
            session_key = key
            cumulative_pv = _ZERO
            cumulative_volume = _ZERO
        typical_price = (candle.high_price + candle.low_price + candle.close_price) / _THREE
        cumulative_pv += typical_price * candle.volume
        cumulative_volume += candle.volume
        results[index] = cumulative_pv / cumulative_volume if cumulative_volume > _ZERO else None
    return tuple(results)


def rsi_series(candles: Sequence[Candle], period: int = 14) -> tuple[Decimal | None, ...]:
    """Relative Strength Index series aligned with newest-first ``candles``.

    Uses Wilder smoothing over chronological close differences: the first RSI
    appears once ``period + 1`` closes exist, seeded with a simple average of
    the gains/losses from only the first ``period`` chronological differences;
    afterwards each remaining difference is smoothed in exactly once with
    ``avg = (previous * (period - 1) + current) / period``.
    A zero denominator maps to 50 (flat), 100 when only positive changes exist,
    and 0 when only negative ones do. Positions before warmup are ``None``.
    All currencies must match. Raises ``ValueError`` when ``period`` is not an
    integer (bools rejected), is zero/negative, or on a currency mismatch.
    """
    checked_period = _validate_positive_int(period, "RSI 기간")
    items = tuple(candles)
    chronological = tuple(reversed(items))
    _require_single_currency(chronological)
    count = len(chronological)
    results: list[Decimal | None] = [None] * count
    if count < checked_period + 1:
        return tuple(results)
    gain_total = _ZERO
    loss_total = _ZERO
    for index in range(checked_period):  # seed uses ONLY the first `period` differences
        change = chronological[index + 1].close_price - chronological[index].close_price
        if change > _ZERO:
            gain_total += change
        else:
            loss_total -= change  # non-positive change: zero or loss magnitude
    avg_gain = gain_total / checked_period
    avg_loss = loss_total / checked_period
    span = checked_period - 1  # Wilder smoothing weight for the previous average
    previous_gain = avg_gain
    previous_loss = avg_loss
    results[checked_period] = _rsi_value(avg_gain, avg_loss)
    for index in range(checked_period + 1, count):
        change = chronological[index].close_price - chronological[index - 1].close_price
        gain = change if change > _ZERO else _ZERO
        loss = -change if change < _ZERO else _ZERO
        previous_gain = (previous_gain * span + gain) / checked_period
        previous_loss = (previous_loss * span + loss) / checked_period
        results[index] = _rsi_value(previous_gain, previous_loss)
    return tuple(reversed(results))


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == _ZERO:
        return _FIFTY if avg_gain == _ZERO else _HUNDRED
    if avg_gain == _ZERO:
        return _ZERO
    return _HUNDRED - _HUNDRED / (_ONE + avg_gain / avg_loss)


def relative_volume(
    candles: Sequence[Candle], lookback: int = 20, minimum_baseline: int = 3
) -> Decimal | None:
    """Latest volume divided by the median of prior strictly-positive volumes.

    Considers up to ``lookback`` candles before the newest one, ignoring
    non-positive volumes from the baseline; returns ``None`` when fewer than
    ``minimum_baseline`` such volumes exist, when the newest candle's own
    volume is not positive, or when there is no newest candle at all. All
    currencies must match. Raises ``ValueError`` when any parameter is not an
    integer (bools rejected), is zero/negative, or exceeds ``lookback``.
    """
    checked_lookback = _validate_positive_int(lookback, "거래량 비교 구간")
    checked_minimum = _validate_positive_int(minimum_baseline, "최소 기준 거래량 개수")
    if checked_minimum > checked_lookback:
        raise ValueError(
            f"최소 기준 거래량 개수는 비교 구간 이하여야 합니다: "
            f"{checked_minimum} > {checked_lookback}"
        )
    items = tuple(candles[: checked_lookback + 1])
    _require_single_currency(items)
    if not items:
        return None
    latest = items[0]
    if latest.volume <= _ZERO:
        return None
    baseline = sorted(candle.volume for candle in items[1:] if candle.volume > _ZERO)
    if len(baseline) < checked_minimum:
        return None
    middle = len(baseline) // 2
    median = (
        baseline[middle]
        if len(baseline) % 2 == 1
        else (baseline[middle - 1] + baseline[middle]) / _TWO
    )
    if median <= _ZERO:
        return None
    return latest.volume / median


@dataclass(frozen=True, slots=True)
class SupportResistance:
    """Optional price levels; every field is ``None`` when unavailable."""

    previous_close: Decimal | None = None
    session_high: Decimal | None = None
    session_low: Decimal | None = None
    recent_high: Decimal | None = None
    recent_low: Decimal | None = None
    swing_high: Decimal | None = None
    swing_low: Decimal | None = None


def _pivot_extremes(
    candles: tuple[Candle, ...], pivot_window: int
) -> tuple[Decimal | None, Decimal | None]:
    """Most recent confirmed strict local pivot high/low with neighbors on both sides.

    ``candles`` must be newest-first (index 0 is newest). A pivot at array
    position ``i`` needs ``pivot_window`` newer neighbors (indices below
    ``i``) and ``pivot_window`` older neighbors (indices above ``i``);
    positions within ``pivot_window`` of either edge can never be confirmed.
    The scan starts at the newest confirmable index and moves toward older
    candles so the first match found is the most recent one. Ties (plateaus)
    are rejected by requiring strictly greater/less than *every* neighbor.
    """
    count = len(candles)
    first_pivot_index = pivot_window  # newest index that has a full newer-side window
    last_pivot_index = count - 1 - pivot_window  # oldest index that has a full older-side window
    swing_high: Decimal | None = None
    swing_low: Decimal | None = None
    for i in range(first_pivot_index, last_pivot_index + 1):
        center_high = candles[i].high_price
        if (
            swing_high is None
            and all(center_high > candles[j].high_price for j in range(i - pivot_window, i))
            and all(center_high > candles[j].high_price for j in range(i + 1, i + pivot_window + 1))
        ):
            swing_high = center_high
        center_low = candles[i].low_price
        if (
            swing_low is None
            and all(center_low < candles[j].low_price for j in range(i - pivot_window, i))
            and all(center_low < candles[j].low_price for j in range(i + 1, i + pivot_window + 1))
        ):
            swing_low = center_low
        if swing_high is not None and swing_low is not None:
            break
    return swing_high, swing_low


def support_resistance(
    candles: Sequence[Candle],
    daily_candles: Sequence[Candle] = (),
    lookback: int = 20,
    pivot_window: int = 2,
) -> SupportResistance:
    """Price levels from newest-first intraday ``candles`` and daily history.

    Session identity is the local date plus UTC offset of the newest intraday
    candle; session high/low cover only matching candles, while recent high/low
    span up to ``lookback`` of them regardless of session. ``previous_close``
    uses the second-newest daily close when the newest daily candle shares the
    intraday session identity (the still-open day), otherwise the newest daily
    close. Swing points are the most recent confirmed strict local extremes
    needing ``pivot_window`` neighbors on both sides. All fields are ``None``
    when their inputs are insufficient. Raises ``ValueError`` for non-positive
    integer parameters, currency mismatches across relevant inputs, or naive
    timestamps where a date comparison is required.
    """
    checked_lookback = _validate_positive_int(lookback, "최근 구간")
    checked_window = _validate_positive_int(pivot_window, "피벗 창")
    items = tuple(candles)
    dailies = tuple(daily_candles)
    _require_single_currency(items)
    _require_single_currency(dailies)
    if items and dailies:
        _require_single_currency((items[0], dailies[0]))
    previous_close: Decimal | None = None
    session_high: Decimal | None = None
    session_low: Decimal | None = None
    recent_high: Decimal | None = None
    recent_low: Decimal | None = None
    if items:
        newest = items[0]
        newest_key = _session_key(_parse_timestamp(newest.timestamp))
        selected = items[:checked_lookback]
        recent_high = max(candle.high_price for candle in selected)
        recent_low = min(candle.low_price for candle in selected)
        session_candles = [
            candle
            for candle in selected
            if _session_key(_parse_timestamp(candle.timestamp)) == newest_key
        ]
        if session_candles:
            session_high = max(candle.high_price for candle in session_candles)
            session_low = min(candle.low_price for candle in session_candles)
        if dailies:
            newest_daily = dailies[0]
            daily_parsed = _parse_timestamp(newest_daily.timestamp)
            same_session = _session_key(daily_parsed) == newest_key
            previous_close = (
                dailies[1].close_price
                if same_session and len(dailies) > 1
                else newest_daily.close_price
            )
    swing_high, swing_low = _pivot_extremes(items, checked_window)
    return SupportResistance(
        previous_close=previous_close,
        session_high=session_high,
        session_low=session_low,
        recent_high=recent_high,
        recent_low=recent_low,
        swing_high=swing_high,
        swing_low=swing_low,
    )


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    """Momentum snapshot plus optional price levels; fields are ``None`` when unavailable."""

    rsi: Decimal | None = None
    relative_volume: Decimal | None = None
    levels: SupportResistance | None = None


def indicator_snapshot(
    candles: Sequence[Candle],
    daily_candles: Sequence[Candle] = (),
    rsi_period: int = 14,
    volume_lookback: int = 20,
    minimum_baseline: int = 3,
    level_lookback: int = 20,
    pivot_window: int = 2,
) -> IndicatorSnapshot:
    """Bundle momentum and price-level indicators for newest-first ``candles``.

    ``rsi`` comes from :func:`rsi_series` (newest position), ``relative_volume``
    from :func:`relative_volume`, and ``levels`` from :func:`support_resistance`
    using the intraday candles plus ``daily_candles``; each field stays ``None``
    when its input is insufficient. All validation (periods, currency, session
    identity) is delegated to those helpers.
    """
    checked_period = _validate_positive_int(rsi_period, "RSI 기간")
    rsi_values = rsi_series(candles, checked_period)
    rsi = rsi_values[0] if rsi_values else None
    relative = relative_volume(candles, volume_lookback, minimum_baseline)
    return IndicatorSnapshot(
        rsi=rsi,
        relative_volume=relative,
        levels=support_resistance(
            candles,
            daily_candles=daily_candles,
            lookback=level_lookback,
            pivot_window=pivot_window,
        ),
    )
