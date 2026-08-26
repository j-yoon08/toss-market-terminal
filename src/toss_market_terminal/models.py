from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
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


# v0.6: account context is limited to the two official settlement currencies.
SUPPORTED_ACCOUNT_CURRENCIES = ("KRW", "USD")


def infer_account_currency(symbol: str) -> str:
    """Infer the settlement currency of a symbol for buying-power lookups.

    KR symbols are exactly 6 digits; everything else is treated as US.
    """
    return "KRW" if infer_market(symbol) == "kr" else "USD"


class DataShapeError(ValueError):
    """The provider returned an unsupported public market-data shape."""


# Official decimal fields are strings with maxLength 30; anything beyond that
# is not a supported shape. The bound also guarantees Decimal parsing can never
# produce astronomically large exponents (finite but unrepresentable digits).
MAX_DECIMAL_TEXT_LENGTH = 30


def as_decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, (str, int)):
        raise DataShapeError(f"{field} 값이 decimal 문자열이 아닙니다.")
    if len(str(value)) > MAX_DECIMAL_TEXT_LENGTH:
        raise DataShapeError(f"{field} 값의 자릿수가 너무 깁니다.")
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


# ---------------------------------------------------------------------------
# v0.6 read-only account context models.
#
# Privacy rules enforced here:
#   * raw account numbers are never stored on any model (masked only),
#   * all decimals are strictly parsed from finite decimal strings/ints,
#   * unknown enum strings are preserved verbatim instead of rejected,
#   * any malformed shape fails closed with DataShapeError (a ValueError).
# ---------------------------------------------------------------------------


def mask_account_no(account_no: str) -> str:
    """Mask an account number so at most the last 4 characters remain."""
    if len(account_no) <= 4:
        return "*" * len(account_no)
    return "*" * (len(account_no) - 4) + account_no[-4:]


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DataShapeError(f"{field} 값은 객체여야 합니다.")
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DataShapeError(f"{field} 값은 비어 있지 않은 문자열이어야 합니다.")
    return value


def _require_decimal_or_none(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_DECIMAL_TEXT_LENGTH:
        raise DataShapeError(f"{field} 값이 decimal 문자열이 아닙니다.")
    return as_decimal(value, field)


def _require_aware_datetime(value: Any, field: str) -> datetime:
    text = _require_str(value, field)
    if len(text) > 64 or text != text.strip():
        raise DataShapeError(f"{field} 값은 유효한 ISO 8601 시각이어야 합니다.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataShapeError(f"{field} 값은 유효한 ISO 8601 시각이어야 합니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataShapeError(f"{field} 값에는 시간대가 포함되어야 합니다.")
    return parsed


def _require_aware_datetime_or_none(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    return _require_aware_datetime(value, field)


def _require_date_or_none(value: Any, field: str) -> date | None:
    if value is None:
        return None
    text = _require_str(value, field)
    if len(text) != 10 or text != text.strip():
        raise DataShapeError(f"{field} 값은 YYYY-MM-DD 형식이어야 합니다.")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DataShapeError(f"{field} 값은 YYYY-MM-DD 형식이어야 합니다.") from exc


def as_account_seq(value: Any, field: str = "accountSeq") -> int:
    """Strictly parse a positive integer account sequence key.

    Booleans, floats, numeric strings and non-positive values are all
    rejected: the provider declares this field as a true JSON integer.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DataShapeError(f"{field} 값은 양의 정수여야 합니다.")
    return value


@dataclass(frozen=True, slots=True)
class CurrencyAmounts:
    """Official ``Price`` schema: currency-scoped sums without FX conversion."""

    krw: Decimal
    usd: Decimal | None

    @classmethod
    def from_api(cls, raw: Any) -> CurrencyAmounts:
        body = _require_dict(raw, "amounts")
        krw = as_decimal(body.get("krw"), "amounts.krw")
        usd = _require_decimal_or_none(body.get("usd"), "amounts.usd")
        return cls(krw=krw, usd=usd)

    def for_currency(self, currency: str) -> Decimal | None:
        if currency == "KRW":
            return self.krw
        if currency == "USD":
            return self.usd
        return None


@dataclass(frozen=True, slots=True)
class Account:
    """Privacy-safe account identity. The raw account number is never stored."""

    account_seq: int
    account_type: str
    masked_account_no: str

    @classmethod
    def from_api(cls, raw: Any) -> Account:
        body = _require_dict(raw, "account")
        try:
            account_no = _require_str(body["accountNo"], "account.accountNo")
            account_seq = as_account_seq(body["accountSeq"])
            account_type = _require_str(body["accountType"], "account.accountType")
        except KeyError as exc:
            raise DataShapeError(f"account 필드가 누락되었습니다: {exc.args[0]}") from exc
        # Unknown enum strings are preserved; only emptiness is invalid.
        return cls(
            account_seq=account_seq,
            account_type=account_type,
            masked_account_no=mask_account_no(account_no),
        )

    @property
    def is_brokerage(self) -> bool:
        return self.account_type == "BROKERAGE"


@dataclass(frozen=True, slots=True)
class Cost:
    commission: Decimal
    tax: Decimal | None

    @classmethod
    def from_api(cls, raw: Any) -> Cost:
        body = _require_dict(raw, "cost")
        if "commission" not in body:
            raise DataShapeError("cost.commission 값이 누락되었습니다.")
        if "tax" not in body:
            raise DataShapeError("cost.tax 값이 누락되었습니다.")
        tax = body["tax"]
        if tax is not None and not isinstance(tax, str):
            raise DataShapeError("cost.tax 값이 decimal 문자열이 아닙니다.")
        return cls(
            commission=as_decimal(body["commission"], "cost.commission"),
            tax=_require_decimal_or_none(tax, "cost.tax"),
        )


@dataclass(frozen=True, slots=True)
class DailyProfitLoss:
    amount: Decimal
    rate: Decimal

    @classmethod
    def from_api(cls, raw: Any, *, amount_field: str = "dailyProfitLoss.amount") -> DailyProfitLoss:
        body = _require_dict(raw, "dailyProfitLoss")
        for key in ("amount", "rate"):
            if key not in body:
                raise DataShapeError(f"dailyProfitLoss.{key} 값이 누락되었습니다.")
        return cls(
            amount=as_decimal(body["amount"], f"{amount_field}.amount"),
            rate=as_decimal(body["rate"], f"{amount_field}.rate"),
        )


@dataclass(frozen=True, slots=True)
class ProfitLoss:
    amount: Decimal
    amount_after_cost: Decimal
    rate: Decimal
    rate_after_cost: Decimal

    @classmethod
    def from_api(cls, raw: Any) -> ProfitLoss:
        body = _require_dict(raw, "profitLoss")
        for key in ("amount", "amountAfterCost", "rate", "rateAfterCost"):
            if key not in body:
                raise DataShapeError(f"profitLoss.{key} 값이 누락되었습니다.")
        return cls(
            amount=as_decimal(body["amount"], "profitLoss.amount"),
            amount_after_cost=as_decimal(body["amountAfterCost"], "profitLoss.amountAfterCost"),
            rate=as_decimal(body["rate"], "profitLoss.rate"),
            rate_after_cost=as_decimal(body["rateAfterCost"], "profitLoss.rateAfterCost"),
        )


@dataclass(frozen=True, slots=True)
class MarketValue:
    purchase_amount: Decimal
    amount: Decimal
    amount_after_cost: Decimal

    @classmethod
    def from_api(cls, raw: Any) -> MarketValue:
        body = _require_dict(raw, "marketValue")
        for key in ("purchaseAmount", "amount", "amountAfterCost"):
            if key not in body:
                raise DataShapeError(f"marketValue.{key} 값이 누락되었습니다.")
        return cls(
            purchase_amount=as_decimal(body["purchaseAmount"], "marketValue.purchaseAmount"),
            amount=as_decimal(body["amount"], "marketValue.amount"),
            amount_after_cost=as_decimal(body["amountAfterCost"], "marketValue.amountAfterCost"),
        )


@dataclass(frozen=True, slots=True)
class HoldingsItem:
    symbol: str
    name: str
    market_country: str
    currency: str
    quantity: Decimal
    last_price: Decimal
    average_purchase_price: Decimal
    market_value: MarketValue
    profit_loss: ProfitLoss
    daily_profit_loss: DailyProfitLoss
    cost: Cost

    @classmethod
    def from_api(cls, raw: Any) -> HoldingsItem:
        body = _require_dict(raw, "holdings.item")
        for key in (
            "symbol",
            "name",
            "marketCountry",
            "currency",
            "quantity",
            "lastPrice",
            "averagePurchasePrice",
        ):
            if key not in body:
                raise DataShapeError(f"holdings.item.{key} 값이 누락되었습니다.")
        return cls(
            symbol=_require_str(body["symbol"], "holdings.item.symbol"),
            name=_require_str(body["name"], "holdings.item.name"),
            market_country=_require_str(body["marketCountry"], "holdings.item.marketCountry"),
            currency=_require_str(body["currency"], "holdings.item.currency"),
            quantity=as_decimal(body["quantity"], "holdings.item.quantity"),
            last_price=as_decimal(body["lastPrice"], "holdings.item.lastPrice"),
            average_purchase_price=as_decimal(
                body["averagePurchasePrice"], "holdings.item.averagePurchasePrice"
            ),
            market_value=MarketValue.from_api(body.get("marketValue")),
            profit_loss=ProfitLoss.from_api(body.get("profitLoss")),
            daily_profit_loss=DailyProfitLoss.from_api(body.get("dailyProfitLoss")),
            cost=Cost.from_api(body.get("cost")),
        )


@dataclass(frozen=True, slots=True)
class OverviewMarketValue:
    amount: CurrencyAmounts
    amount_after_cost: CurrencyAmounts

    @classmethod
    def from_api(cls, raw: Any) -> OverviewMarketValue:
        body = _require_dict(raw, "holdings.marketValue")
        for key in ("amount", "amountAfterCost"):
            if key not in body:
                raise DataShapeError(f"marketValue.{key} 값이 누락되었습니다.")
        return cls(
            amount=CurrencyAmounts.from_api(body["amount"]),
            amount_after_cost=CurrencyAmounts.from_api(body["amountAfterCost"]),
        )


@dataclass(frozen=True, slots=True)
class OverviewProfitLoss:
    amount: CurrencyAmounts
    amount_after_cost: CurrencyAmounts
    rate: Decimal
    rate_after_cost: Decimal

    @classmethod
    def from_api(cls, raw: Any) -> OverviewProfitLoss:
        body = _require_dict(raw, "holdings.profitLoss")
        for key in ("amount", "amountAfterCost", "rate", "rateAfterCost"):
            if key not in body:
                raise DataShapeError(f"profitLoss.{key} 값이 누락되었습니다.")
        return cls(
            amount=CurrencyAmounts.from_api(body["amount"]),
            amount_after_cost=CurrencyAmounts.from_api(body["amountAfterCost"]),
            rate=as_decimal(body["rate"], "overviewProfitLoss.rate"),
            rate_after_cost=as_decimal(body["rateAfterCost"], "overviewProfitLoss.rateAfterCost"),
        )


@dataclass(frozen=True, slots=True)
class OverviewDailyProfitLoss:
    amount: CurrencyAmounts
    rate: Decimal

    @classmethod
    def from_api(cls, raw: Any) -> OverviewDailyProfitLoss:
        body = _require_dict(raw, "holdings.dailyProfitLoss")
        for key in ("amount", "rate"):
            if key not in body:
                raise DataShapeError(f"dailyProfitLoss.{key} 값이 누락되었습니다.")
        return cls(
            amount=CurrencyAmounts.from_api(body["amount"]),
            rate=as_decimal(body["rate"], "overviewDailyProfitLoss.rate"),
        )


@dataclass(frozen=True, slots=True)
class HoldingsOverview:
    total_purchase_amount: CurrencyAmounts
    market_value: OverviewMarketValue
    profit_loss: OverviewProfitLoss
    daily_profit_loss: OverviewDailyProfitLoss
    items: tuple[HoldingsItem, ...]

    @classmethod
    def from_api(cls, raw: Any) -> HoldingsOverview:
        body = _require_dict(raw, "holdings.overview")
        for key in ("totalPurchaseAmount", "marketValue", "profitLoss", "dailyProfitLoss"):
            if key not in body:
                raise DataShapeError(f"holdings.{key} 값이 누락되었습니다.")
        items_raw = body.get("items")
        if not isinstance(items_raw, list):
            raise DataShapeError("holdings.items 값은 배열이어야 합니다.")
        return cls(
            total_purchase_amount=CurrencyAmounts.from_api(body["totalPurchaseAmount"]),
            market_value=OverviewMarketValue.from_api(body["marketValue"]),
            profit_loss=OverviewProfitLoss.from_api(body["profitLoss"]),
            daily_profit_loss=OverviewDailyProfitLoss.from_api(body["dailyProfitLoss"]),
            items=tuple(HoldingsItem.from_api(item) for item in items_raw),
        )

    def find_item(self, symbol: str) -> HoldingsItem | None:
        normalized = symbol.strip().upper()
        for item in self.items:
            if item.symbol.upper() == normalized:
                return item
        return None


@dataclass(frozen=True, slots=True)
class BuyingPower:
    currency: str
    cash_buying_power: Decimal

    @classmethod
    def from_api(cls, raw: Any) -> BuyingPower:
        body = _require_dict(raw, "buyingPower")
        currency = _require_str(body.get("currency"), "buyingPower.currency")
        if "cashBuyingPower" not in body:
            raise DataShapeError("buyingPower.cashBuyingPower 값이 누락되었습니다.")
        # Unknown currency strings are preserved here; callers restrict to KRW/USD.
        return cls(
            currency=currency,
            cash_buying_power=as_decimal(body["cashBuyingPower"], "buyingPower.cashBuyingPower"),
        )


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Privacy-safe read-only account context for one symbol.

    ``scope`` and ``order_endpoints_called`` make the boundary explicit in
    output; the account number only ever appears in masked form.
    """

    scope: str
    order_endpoints_called: bool
    account: Account
    symbol: str
    holding: HoldingsItem | None
    holding_quantity: Decimal
    buying_power: BuyingPower


# ---------------------------------------------------------------------------
# v0.8c read-only open orders + duplicate detection.
#
# Kept deliberately minimal: only what duplicate detection and basic order
# identification need. ``status``/``orderType`` are preserved verbatim (the
# official schema explicitly requires clients to tolerate unknown enum
# values there); ``side`` is the one genuinely closed enum on this schema,
# so it is validated strictly since duplicate polarity depends on it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    price: Decimal | None
    filled_quantity: Decimal
    remaining_quantity: Decimal

    @classmethod
    def from_api(cls, raw: Any) -> OpenOrder:
        body = _require_dict(raw, "order")
        for key in ("orderId", "symbol", "side", "orderType", "status", "quantity", "execution"):
            if key not in body:
                raise DataShapeError(f"order.{key} 값이 누락되었습니다.")
        side = body["side"]
        if side not in ("BUY", "SELL"):
            raise DataShapeError("order.side 값은 BUY 또는 SELL이어야 합니다.")
        quantity = as_decimal(body["quantity"], "order.quantity")
        execution = _require_dict(body["execution"], "order.execution")
        if "filledQuantity" not in execution:
            raise DataShapeError("order.execution.filledQuantity 값이 누락되었습니다.")
        filled_quantity = as_decimal(execution["filledQuantity"], "order.execution.filledQuantity")
        if filled_quantity < 0 or filled_quantity > quantity:
            # accepted != filled: a filled amount outside [0, quantity] is an
            # unsupported shape rather than a value this client can trust.
            raise DataShapeError("order.execution.filledQuantity 값이 올바르지 않습니다.")
        return cls(
            order_id=_require_str(body["orderId"], "order.orderId"),
            symbol=_require_str(body["symbol"], "order.symbol"),
            side=side,
            order_type=_require_str(body["orderType"], "order.orderType"),
            status=_require_str(body["status"], "order.status"),
            quantity=quantity,
            price=_require_decimal_or_none(body.get("price"), "order.price"),
            filled_quantity=filled_quantity,
            remaining_quantity=quantity - filled_quantity,
        )


@dataclass(frozen=True, slots=True)
class OpenOrdersPage:
    """A fully-materialized OPEN-status page: no cursor, nothing left to fetch."""

    orders: tuple[OpenOrder, ...]

    @classmethod
    def from_api(cls, raw: Any) -> OpenOrdersPage:
        body = _require_dict(raw, "orders.page")
        for key in ("orders", "nextCursor", "hasNext"):
            if key not in body:
                raise DataShapeError(f"orders.page.{key} 값이 누락되었습니다.")
        orders_raw = body["orders"]
        if not isinstance(orders_raw, list):
            raise DataShapeError("orders.page.orders 값은 배열이어야 합니다.")
        if body["nextCursor"] is not None:
            raise DataShapeError("OPEN 조회 응답의 nextCursor는 null이어야 합니다.")
        has_next = body["hasNext"]
        if not isinstance(has_next, bool) or has_next:
            raise DataShapeError("OPEN 조회 응답의 hasNext는 false여야 합니다.")
        return cls(orders=tuple(OpenOrder.from_api(item) for item in orders_raw))


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    """Privacy-safe USD/KRW reference rate returned by the official API."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    mid_rate: Decimal
    basis_point: Decimal
    rate_change_type: str
    valid_from: datetime
    valid_until: datetime

    @classmethod
    def from_api(cls, raw: Any) -> ExchangeRate:
        body = _require_dict(raw, "exchangeRate")
        for key in (
            "baseCurrency",
            "quoteCurrency",
            "rate",
            "midRate",
            "basisPoint",
            "rateChangeType",
            "validFrom",
            "validUntil",
        ):
            if key not in body:
                raise DataShapeError(f"exchangeRate.{key} 값이 누락되었습니다.")
        base = _require_str(body["baseCurrency"], "exchangeRate.baseCurrency")
        quote = _require_str(body["quoteCurrency"], "exchangeRate.quoteCurrency")
        if (base, quote) != ("USD", "KRW"):
            raise DataShapeError("USD/KRW 환율 응답만 지원합니다.")
        rate = as_decimal(body["rate"], "exchangeRate.rate")
        mid_rate = as_decimal(body["midRate"], "exchangeRate.midRate")
        if rate <= 0 or mid_rate <= 0:
            raise DataShapeError("exchangeRate 환율은 양수여야 합니다.")
        change_type = _require_str(body["rateChangeType"], "exchangeRate.rateChangeType")
        if change_type not in {"UP", "EQUAL", "DOWN"}:
            raise DataShapeError("exchangeRate.rateChangeType 값이 올바르지 않습니다.")
        valid_from = _require_aware_datetime(body["validFrom"], "exchangeRate.validFrom")
        valid_until = _require_aware_datetime(body["validUntil"], "exchangeRate.validUntil")
        if valid_until <= valid_from:
            raise DataShapeError("exchangeRate 유효 종료 시각은 시작 시각보다 늦어야 합니다.")
        return cls(
            base_currency=base,
            quote_currency=quote,
            rate=rate,
            mid_rate=mid_rate,
            basis_point=as_decimal(body["basisPoint"], "exchangeRate.basisPoint"),
            rate_change_type=change_type,
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class ClosedOrder:
    """Recent closed-order display record that intentionally omits broker order IDs."""

    symbol: str
    side: str
    order_type: str
    status: str
    currency: str
    quantity: Decimal
    price: Decimal | None
    ordered_at: datetime
    filled_quantity: Decimal
    average_filled_price: Decimal | None
    filled_amount: Decimal | None
    commission: Decimal | None
    tax: Decimal | None
    filled_at: datetime | None
    settlement_date: date | None

    @classmethod
    def from_api(cls, raw: Any) -> ClosedOrder:
        body = _require_dict(raw, "closedOrder")
        for key in (
            "orderId",
            "symbol",
            "side",
            "orderType",
            "status",
            "currency",
            "quantity",
            "price",
            "orderedAt",
            "execution",
        ):
            if key not in body:
                raise DataShapeError(f"closedOrder.{key} 값이 누락되었습니다.")
        # Validate the official shape, then deliberately discard this value:
        # broker order IDs must not survive into the display model.
        _require_str(body["orderId"], "closedOrder.orderId")
        side = body["side"]
        if side not in ("BUY", "SELL"):
            raise DataShapeError("closedOrder.side 값은 BUY 또는 SELL이어야 합니다.")
        currency = _require_str(body["currency"], "closedOrder.currency")
        if currency not in {"KRW", "USD"}:
            raise DataShapeError("closedOrder.currency는 KRW 또는 USD여야 합니다.")
        quantity = as_decimal(body["quantity"], "closedOrder.quantity")
        if quantity <= 0:
            raise DataShapeError("closedOrder.quantity는 양수여야 합니다.")
        execution = _require_dict(body["execution"], "closedOrder.execution")
        required_execution = (
            "filledQuantity",
            "averageFilledPrice",
            "filledAmount",
            "commission",
            "tax",
            "filledAt",
            "settlementDate",
        )
        for key in required_execution:
            if key not in execution:
                raise DataShapeError(f"closedOrder.execution.{key} 값이 누락되었습니다.")
        filled = as_decimal(execution["filledQuantity"], "closedOrder.execution.filledQuantity")
        if filled < 0 or filled > quantity:
            raise DataShapeError("closedOrder.execution.filledQuantity 값이 올바르지 않습니다.")
        return cls(
            symbol=normalize_symbol(_require_str(body["symbol"], "closedOrder.symbol")),
            side=side,
            order_type=_require_str(body["orderType"], "closedOrder.orderType"),
            status=_require_str(body["status"], "closedOrder.status"),
            currency=currency,
            quantity=quantity,
            price=_require_decimal_or_none(body.get("price"), "closedOrder.price"),
            ordered_at=_require_aware_datetime(body["orderedAt"], "closedOrder.orderedAt"),
            filled_quantity=filled,
            average_filled_price=_require_decimal_or_none(
                execution["averageFilledPrice"], "closedOrder.execution.averageFilledPrice"
            ),
            filled_amount=_require_decimal_or_none(
                execution["filledAmount"], "closedOrder.execution.filledAmount"
            ),
            commission=_require_decimal_or_none(
                execution["commission"], "closedOrder.execution.commission"
            ),
            tax=_require_decimal_or_none(execution["tax"], "closedOrder.execution.tax"),
            filled_at=_require_aware_datetime_or_none(
                execution["filledAt"], "closedOrder.execution.filledAt"
            ),
            settlement_date=_require_date_or_none(
                execution["settlementDate"], "closedOrder.execution.settlementDate"
            ),
        )


@dataclass(frozen=True, slots=True)
class ClosedOrdersPage:
    """Bounded first page of CLOSED orders; pagination cursor is never retained."""

    orders: tuple[ClosedOrder, ...]
    has_more: bool

    @classmethod
    def from_api(cls, raw: Any) -> ClosedOrdersPage:
        body = _require_dict(raw, "closedOrders.page")
        for key in ("orders", "nextCursor", "hasNext"):
            if key not in body:
                raise DataShapeError(f"closedOrders.page.{key} 값이 누락되었습니다.")
        orders_raw = body["orders"]
        if not isinstance(orders_raw, list):
            raise DataShapeError("closedOrders.page.orders 값은 배열이어야 합니다.")
        if len(orders_raw) > 20:
            raise DataShapeError("종료 주문 응답은 최대 20건이어야 합니다.")
        has_next = body["hasNext"]
        if not isinstance(has_next, bool):
            raise DataShapeError("closedOrders.page.hasNext 값은 boolean이어야 합니다.")
        cursor = body["nextCursor"]
        if has_next:
            _require_str(cursor, "closedOrders.page.nextCursor")
        elif cursor is not None:
            raise DataShapeError("종료 주문 마지막 페이지의 nextCursor는 null이어야 합니다.")
        return cls(
            orders=tuple(ClosedOrder.from_api(item) for item in orders_raw),
            has_more=has_next,
        )


# Official OrderStatus values that unambiguously mean an order is no longer
# open. Everything else -- PENDING/PARTIAL_FILLED/PENDING_CANCEL/
# PENDING_REPLACE, or any status string this client has never seen -- fails
# closed as a possible duplicate instead of being silently excluded.
TERMINAL_OPEN_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELED", "REJECTED", "REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED"}
)


def find_open_order_duplicates(
    orders: tuple[OpenOrder, ...], symbol: str, side: str
) -> tuple[OpenOrder, ...]:
    """Pure lookup for existing open orders that could collide with a new one.

    Matches on normalized symbol and exact side. A status is excluded only
    when it is a definitely-terminal official status; every active or
    unrecognized status is kept as a possible duplicate (fail closed).
    """
    normalized_symbol = normalize_symbol(symbol)
    if side not in ("BUY", "SELL"):
        raise ValueError("side는 BUY 또는 SELL이어야 합니다.")
    return tuple(
        order
        for order in orders
        if order.symbol.strip().upper() == normalized_symbol
        and order.side == side
        and order.status not in TERMINAL_OPEN_ORDER_STATUSES
    )


# ---------------------------------------------------------------------------
# Phase-1 portfolio snapshot: one immutable, privacy-safe read of everything
# the portfolio screen needs (account identity, KRW/USD buying power, full
# holdings, all OPEN orders) plus a deterministic sync instant. Assembled by
# ``TossMarketClient.portfolio_snapshot`` from already-validated GET-only
# results; it carries no fields this module cannot already prove are safe.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    scope: str
    order_endpoints_called: bool
    account: Account
    krw_buying_power: BuyingPower
    usd_buying_power: BuyingPower
    holdings: HoldingsOverview
    open_orders: OpenOrdersPage
    synced_at: datetime
