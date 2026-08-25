from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
# Official batch limit for the read-only /api/v1/prices endpoint.
MAX_BATCH_SYMBOLS = 200


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("종목 심볼 형식이 올바르지 않습니다.")
    return normalized


def infer_market(symbol: str) -> str:
    return "kr" if len(symbol) == 6 and symbol.isdigit() else "us"


class DataShapeError(ValueError):
    """The provider returned an unsupported public market-data shape."""


def as_decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, (str, int)):
        raise DataShapeError(f"{field} 값이 decimal 문자열이 아닙니다.")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise DataShapeError(f"{field} 값을 Decimal로 변환할 수 없습니다.") from exc
    if not parsed.is_finite():
        raise DataShapeError(f"{field} 값은 유한한 수여야 합니다.")
    return parsed


@dataclass(frozen=True, slots=True)
class StockInfo:
    symbol: str
    name: str
    english_name: str
    market: str
    currency: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> StockInfo:
        return cls(
            symbol=str(raw["symbol"]),
            name=str(raw.get("name") or raw["symbol"]),
            english_name=str(raw.get("englishName") or ""),
            market=str(raw.get("market") or "UNKNOWN"),
            currency=str(raw.get("currency") or "UNKNOWN"),
        )


@dataclass(frozen=True, slots=True)
class Price:
    symbol: str
    last_price: Decimal
    currency: str
    timestamp: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Price:
        return cls(
            symbol=str(raw["symbol"]),
            last_price=as_decimal(raw["lastPrice"], "lastPrice"),
            currency=str(raw.get("currency") or "UNKNOWN"),
            timestamp=str(raw["timestamp"]) if raw.get("timestamp") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class OrderbookEntry:
    price: Decimal
    volume: Decimal

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> OrderbookEntry:
        return cls(
            price=as_decimal(raw["price"], "orderbook.price"),
            volume=as_decimal(raw["volume"], "orderbook.volume"),
        )


@dataclass(frozen=True, slots=True)
class Orderbook:
    currency: str
    asks: tuple[OrderbookEntry, ...]
    bids: tuple[OrderbookEntry, ...]
    timestamp: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Orderbook:
        return cls(
            currency=str(raw.get("currency") or "UNKNOWN"),
            asks=tuple(OrderbookEntry.from_api(item) for item in raw.get("asks", [])),
            bids=tuple(OrderbookEntry.from_api(item) for item in raw.get("bids", [])),
            timestamp=str(raw["timestamp"]) if raw.get("timestamp") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Trade:
    price: Decimal
    volume: Decimal
    timestamp: str
    currency: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Trade:
        return cls(
            price=as_decimal(raw["price"], "trade.price"),
            volume=as_decimal(raw["volume"], "trade.volume"),
            timestamp=str(raw["timestamp"]),
            currency=str(raw.get("currency") or "UNKNOWN"),
        )


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: str
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Candle:
        return cls(
            timestamp=str(raw["timestamp"]),
            open_price=as_decimal(raw["openPrice"], "candle.openPrice"),
            high_price=as_decimal(raw["highPrice"], "candle.highPrice"),
            low_price=as_decimal(raw["lowPrice"], "candle.lowPrice"),
            close_price=as_decimal(raw["closePrice"], "candle.closePrice"),
            volume=as_decimal(raw["volume"], "candle.volume"),
            currency=str(raw.get("currency") or "UNKNOWN"),
        )


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    stock: StockInfo
    price: Price
    orderbook: Orderbook
    trades: tuple[Trade, ...]
    candles: tuple[Candle, ...]
    daily_candles: tuple[Candle, ...] = ()
