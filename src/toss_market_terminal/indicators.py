"""Candle aggregation helpers for chart timeframes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from toss_market_terminal.models import Candle

SUPPORTED_TIMEFRAMES = ("1m", "5m")
_BUCKET_MINUTES = {"1m": 1, "5m": 5}
_MAX_ECHO = 32


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


def _bucket_key(start: datetime) -> tuple[int, int]:
    # Local date plus wall-clock minute-of-day keeps buckets from crossing dates.
    return start.date().toordinal(), start.hour * 60 + start.minute


def aggregate_candles(candles: Sequence[Candle], timeframe: str) -> tuple[Candle, ...]:
    """Aggregate newest-first 1m ``candles`` into ``timeframe`` candles (newest-first).

    Supported timeframes: ``1m`` (identity) and ``5m``. Buckets follow the local
    wall clock (date + five-minute boundary) and never cross dates; an incomplete
    latest bucket is included. OHLC uses the earliest open, latest close, highest
    high, lowest low; volume is summed. All currencies must match. Raises
    ``ValueError`` for malformed or naive timestamps, currency mismatch, or an
    unsupported timeframe.
    """
    bucket_minutes = _BUCKET_MINUTES.get(timeframe)
    if bucket_minutes is None:
        raise ValueError(f"지원하지 않는 타임프레임입니다: {_echo(timeframe)} (지원: 1m, 5m)")
    items = tuple(candles)
    if timeframe == "1m":
        return items

    buckets: dict[tuple[int, int], list[tuple[datetime, datetime, Candle]]] = {}
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
                volume=sum((candle.volume for _, _, candle in members), Decimal("0")),
                currency=str(currency),
            )
        )
    return tuple(aggregated)
