"""Pure chart calculation helpers: timeframe aggregation, EMA, session VWAP."""

from __future__ import annotations

from collections.abc import Sequence
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


def _validate_period(period: object) -> int:
    if isinstance(period, bool) or not isinstance(period, int):
        raise ValueError(f"EMA 기간은 정수여야 합니다: {_echo(period)}")
    if period <= 0:
        raise ValueError(f"EMA 기간은 양수여야 합니다: {period}")
    return period


def ema_series(candles: Sequence[Candle], period: int) -> tuple[Decimal | None, ...]:
    """Exponential moving average series aligned with newest-first ``candles``.

    Positions before ``period`` closes are ``None``. Seeding is a chronological
    simple average of the first ``period`` closes; afterwards each value is
    ``close * alpha + previous * (1 - alpha)`` with Decimal ``alpha = 2 /
    (period + 1)``. All currencies must match. Raises ``ValueError`` when
    ``period`` is not an integer (bools rejected), is zero/negative, or on a
    currency mismatch.
    """
    checked_period = _validate_period(period)
    items = tuple(candles)
    chronological = tuple(reversed(items))
    currency: str | None = None
    for candle in chronological:
        if currency is None:
            currency = candle.currency
        elif candle.currency != currency:
            raise ValueError(
                f"캔들 통화가 일치하지 않습니다: {_echo(currency)} != {_echo(candle.currency)}"
            )
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
    currency: str | None = None
    for candle in chronological:
        if currency is None:
            currency = candle.currency
        elif candle.currency != currency:
            raise ValueError(
                f"캔들 통화가 일치하지 않습니다: {_echo(currency)} != {_echo(candle.currency)}"
            )
    cumulative_pv = _ZERO
    cumulative_volume = _ZERO
    session_key: tuple[int, int] | None = None
    results: list[Decimal | None] = [None] * len(items)
    for index in range(len(items) - 1, -1, -1):  # chronological walk over newest-first input
        candle = items[index]
        parsed = _parse_timestamp(candle.timestamp)
        key = (_offset_minutes(parsed), parsed.date().toordinal())
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
