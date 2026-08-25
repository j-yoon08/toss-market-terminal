from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timezone
from decimal import Decimal
from typing import Literal

from .models import Candle, Trade

CandleInterval = Literal["1m", "1d"]
_MAX_CANDLES = 200
_ZERO = Decimal("0")


def _parse_aware_timestamp(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("실시간 차트 timestamp 형식이 올바르지 않습니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("실시간 차트 timestamp에는 timezone이 필요합니다.")
    return parsed


def _minute_key(parsed: datetime) -> datetime:
    return parsed.astimezone(UTC).replace(second=0, microsecond=0)


def _daily_key(parsed: datetime, reference: datetime | None) -> tuple[int, int]:
    if reference is not None:
        offset = reference.utcoffset()
        if offset is not None:
            parsed = parsed.astimezone(timezone(offset))
    return parsed.date().toordinal(), int(
        (parsed.utcoffset() or UTC.utcoffset(None)).total_seconds()
    )


def _bucket_key(parsed: datetime, interval: CandleInterval, reference: datetime | None) -> object:
    if interval == "1m":
        return _minute_key(parsed)
    return _daily_key(parsed, reference)


def _bucket_timestamp(
    parsed: datetime, interval: CandleInterval, reference: datetime | None = None
) -> str:
    if interval == "1m":
        return parsed.replace(second=0, microsecond=0).isoformat()
    if reference is not None:
        offset = reference.utcoffset()
        if offset is not None:
            parsed = parsed.astimezone(timezone(offset))
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def apply_trade_to_candles(
    candles: Sequence[Candle],
    trade: Trade,
    *,
    interval: CandleInterval = "1m",
    limit: int = _MAX_CANDLES,
) -> tuple[Candle, ...]:
    """Fold one live trade into newest-first candles without fabricating gaps.

    A trade in the newest bucket updates that bucket's H/L/C and adds volume.
    A newer bucket prepends a fresh candle; an older bucket is ignored. The
    provider snapshot remains the periodic authority that corrects overlap or
    missing WebSocket events.
    """
    if interval not in {"1m", "1d"}:
        raise ValueError("실시간 차트 간격은 1m 또는 1d여야 합니다.")
    if not 1 <= limit <= _MAX_CANDLES:
        raise ValueError("실시간 차트 캔들 수는 1~200개여야 합니다.")
    if trade.price <= _ZERO or trade.volume < _ZERO:
        raise ValueError("실시간 체결 가격과 거래량이 올바르지 않습니다.")

    items = tuple(candles)
    trade_time = _parse_aware_timestamp(trade.timestamp)
    if not items:
        return (
            Candle(
                timestamp=_bucket_timestamp(trade_time, interval),
                open_price=trade.price,
                high_price=trade.price,
                low_price=trade.price,
                close_price=trade.price,
                volume=trade.volume,
                currency=trade.currency,
            ),
        )

    newest = items[0]
    if newest.currency != trade.currency:
        raise ValueError("실시간 체결과 캔들의 통화가 일치하지 않습니다.")
    newest_time = _parse_aware_timestamp(newest.timestamp)
    trade_key = _bucket_key(trade_time, interval, newest_time)
    newest_key = _bucket_key(newest_time, interval, newest_time)
    if trade_key < newest_key:
        return items
    if trade_key == newest_key:
        return (
            replace(
                newest,
                high_price=max(newest.high_price, trade.price),
                low_price=min(newest.low_price, trade.price),
                close_price=trade.price,
                volume=newest.volume + trade.volume,
            ),
            *items[1:],
        )[:limit]

    fresh = Candle(
        timestamp=_bucket_timestamp(trade_time, interval, newest_time),
        open_price=trade.price,
        high_price=trade.price,
        low_price=trade.price,
        close_price=trade.price,
        volume=trade.volume,
        currency=trade.currency,
    )
    return (fresh, *items)[:limit]
