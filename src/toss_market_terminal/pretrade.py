"""v0.13 pre-trade facts layer.

이 모듈은 PAPER 확인 화면과 최종 LIVE 승인 화면에 보여줄 **사전 점검 사실
(pretrade facts)**을 계산하는 순수 도메인 계층입니다.

경계 선언:
  * 네트워크 호출을 하지 않고, 어떤 클라이언트/전송 계층에도 의존하지 않습니다.
  * Textual에 의존하지 않습니다(Textual 없이도 단위 테스트가 가능해야 합니다).
  * 새로운 리스크 엔진이 아닙니다 -- 이미 존재하는 캐노니컬 입력(주문 의도,
    호가창, 미체결 주문, 신선도 판정)을 그대로 투영해 보여줄 뿐이며, 스스로
    임계값을 만들지 않습니다. BLOCK 상태는 항상 이미 존재하는 차단 조건과
    대응합니다(매수가능금액 초과 -> ``RiskGate``, 보유 수량 초과 -> ``RiskGate``,
    동일 종목·방향 미체결 주문 -> ``find_open_order_duplicates``, 시세 신선도
    저하 -> 호출자가 넘겨주는 캐노니컬 판정).
  * 알 수 없는 값은 0으로 조작하지 않고 명시적으로 UNAVAILABLE로 표시합니다.
  * 계좌번호 원문·account_seq·토큰·주문 ID·전체 승인 문구/지문은 어떤 필드에도
    담기지 않습니다(privacy-safe).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from .models import OpenOrder, Orderbook, find_open_order_duplicates
from .order_preview import (
    OrderIntent,
    OrderSide,
    OrderType,
    RiskGate,
    canonical_decimal_text,
)
from .render import format_decimal, format_percent

__all__ = [
    "FactRow",
    "FactStatus",
    "PretradeFacts",
    "PretradeFactsError",
    "QuoteFactsContext",
    "build_pretrade_facts",
]


class PretradeFactsError(ValueError):
    """사실 계산 입력이 malformed/out-of-domain이라 fail-closed로 거부됨."""


class FactStatus(Enum):
    """네 가지 표시 상태. 순서는 심각도 오름차순이 아니라 표시 우선순위다."""

    # Bandit B105: enum status label, not credential material.
    PASS = "PASS"  # nosec B105
    WARN = "WARN"
    BLOCK = "BLOCK"
    UNAVAILABLE = "UNAVAILABLE"


_STATUS_LABEL_KO: dict[FactStatus, str] = {
    FactStatus.PASS: "정상",
    FactStatus.WARN: "주의",
    FactStatus.BLOCK: "차단",
    FactStatus.UNAVAILABLE: "정보 없음",
}

#: BLOCK이 가장 눈에 띄어야 하고, PASS는 가장 덜 시급하다.
_STATUS_PRIORITY: dict[FactStatus, int] = {
    FactStatus.BLOCK: 3,
    FactStatus.WARN: 2,
    FactStatus.UNAVAILABLE: 1,
    FactStatus.PASS: 0,
}


@dataclass(frozen=True, slots=True)
class FactRow:
    """한 줄짜리 사실. ``label``/``detail``은 이미 완성된 한국어 문구다."""

    key: str
    label: str
    status: FactStatus
    detail: str

    @property
    def status_label(self) -> str:
        return _STATUS_LABEL_KO[self.status]

    def display_line(self) -> str:
        return f"{self.label} · {self.status_label} · {self.detail}"


@dataclass(frozen=True, slots=True)
class PretradeFacts:
    """한 주문 의도에 대한 불변 사실 묶음. 계산 시각(``as_of``)까지 기록한다."""

    symbol: str
    mode: Literal["PAPER", "LIVE"]
    side: OrderSide
    order_type: OrderType
    as_of: datetime
    rows: tuple[FactRow, ...]

    @property
    def overall_status(self) -> FactStatus:
        if not self.rows:
            return FactStatus.UNAVAILABLE
        return max(self.rows, key=lambda row: _STATUS_PRIORITY[row.status]).status

    @property
    def has_block(self) -> bool:
        return any(row.status is FactStatus.BLOCK for row in self.rows)

    @property
    def overall_label(self) -> str:
        return {
            FactStatus.PASS: "정상",
            FactStatus.WARN: "주의",
            FactStatus.BLOCK: "차단",
            FactStatus.UNAVAILABLE: "부분 확인",
        }[self.overall_status]

    def row(self, key: str) -> FactRow | None:
        for row in self.rows:
            if row.key == key:
                return row
        return None

    def to_privacy_safe_lines(self) -> tuple[str, ...]:
        """계좌/토큰/주문ID/지문이 절대 섞이지 않는 표시용 텍스트."""
        return tuple(row.display_line() for row in self.rows)


@dataclass(frozen=True, slots=True)
class QuoteFactsContext:
    """이미 메모리에 있는 시세/호가창 입력. 이 모듈은 아무 것도 조회하지 않는다.

    ``quote_fresh``는 호출자의 캐노니컬 신선도 판정(예: 앱의
    ``_interpretation_is_stale()`` 결과의 부정)을 그대로 넘겨받는다 -- 이 모듈은
    별도의 신선도 임계값을 만들지 않는다.
    """

    orderbook: Orderbook | None
    last_trade_price: Decimal | None
    quote_timestamp: str | None
    quote_fresh: bool
    orderbook_fresh: bool


def _require_finite(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PretradeFactsError(f"{field} 값은 유한한 Decimal이어야 합니다.")
    return value


def _require_positive(value: Decimal, field: str) -> Decimal:
    value = _require_finite(value, field)
    if value <= 0:
        raise PretradeFactsError(f"{field} 값은 0보다 커야 합니다.")
    return value


def _require_nonnegative(value: Decimal, field: str) -> Decimal:
    value = _require_finite(value, field)
    if value < 0:
        raise PretradeFactsError(f"{field} 값은 0 이상이어야 합니다.")
    return value


def _optional_positive(value: Decimal | None, field: str) -> Decimal | None:
    if value is None:
        return None
    return _require_positive(value, field)


def _require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PretradeFactsError(f"{field} 값은 타임존 정보가 있는 datetime이어야 합니다.")
    return value


def _parse_quote_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PretradeFactsError("quote_timestamp 값은 비어 있지 않은 문자열이어야 합니다.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PretradeFactsError("quote_timestamp 값이 유효한 ISO 8601 시각이 아닙니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PretradeFactsError("quote_timestamp 값에는 시간대가 포함되어야 합니다.")
    return parsed


def _format_age_seconds(seconds: float) -> str:
    bounded = max(0.0, seconds)
    return f"{bounded:.1f}초"


def _best_bid_ask(
    orderbook: Orderbook | None, currency: str
) -> tuple[Decimal | None, Decimal | None]:
    if orderbook is None:
        return None, None
    if orderbook.currency != currency:
        return None, None
    best_bid = _optional_positive(orderbook.bids[0].price if orderbook.bids else None, "best_bid")
    best_ask = _optional_positive(orderbook.asks[0].price if orderbook.asks else None, "best_ask")
    return best_bid, best_ask


def _quote_freshness_row(quote: QuoteFactsContext, now: datetime) -> FactRow:
    if quote.quote_timestamp is None:
        age_text = "정보시각 없음"
    else:
        observed = _parse_quote_timestamp(quote.quote_timestamp)
        age_seconds = (now - observed).total_seconds()
        age_text = f"정보시각 기준 {_format_age_seconds(age_seconds)} 경과"
    if quote.quote_fresh:
        return FactRow(
            key="quote_freshness",
            label="시세 신선도",
            status=FactStatus.PASS,
            detail=age_text,
        )
    return FactRow(
        key="quote_freshness",
        label="시세 신선도",
        status=FactStatus.BLOCK,
        detail=f"신선도 저하 · PAPER/LIVE 주문 경로 차단 · {age_text}",
    )


def _best_quote_row(
    orderbook: Orderbook | None, currency: str, *, orderbook_fresh: bool, now: datetime
) -> FactRow:
    if orderbook is not None and orderbook.currency != currency:
        return FactRow(
            key="best_quote",
            label="최우선 호가",
            status=FactStatus.UNAVAILABLE,
            detail="주문 통화와 호가창 통화가 일치하지 않음",
        )
    if orderbook is not None and not orderbook_fresh:
        age_text = "정보시각 없음"
        if orderbook.timestamp is not None:
            observed = _parse_quote_timestamp(orderbook.timestamp)
            age_text = (
                f"호가 정보시각 기준 {_format_age_seconds((now - observed).total_seconds())} 경과"
            )
        return FactRow(
            key="best_quote",
            label="최우선 호가",
            status=FactStatus.UNAVAILABLE,
            detail=f"호가 신선도 저하 · MARKET 기준가로 사용하지 않음 · {age_text}",
        )
    best_bid, best_ask = _best_bid_ask(orderbook, currency)
    if best_bid is None or best_ask is None:
        return FactRow(
            key="best_quote",
            label="최우선 호가",
            status=FactStatus.UNAVAILABLE,
            detail="호가창 정보 없음",
        )
    spread = best_ask - best_bid
    midpoint = (best_ask + best_bid) / Decimal("2")
    if spread < 0:
        return FactRow(
            key="best_quote",
            label="최우선 호가",
            status=FactStatus.WARN,
            detail=(
                f"매수 {format_decimal(best_bid, currency)} · "
                f"매도 {format_decimal(best_ask, currency)} "
                f"{currency} · 호가 역전(비정상 데이터) · MARKET 기준가로 사용하지 않음"
            ),
        )
    spread_percent = spread / midpoint * Decimal("100") if midpoint > 0 else None
    spread_text = format_decimal(spread, currency)
    percent_text = f" ({format_percent(spread_percent)})" if spread_percent is not None else ""
    return FactRow(
        key="best_quote",
        label="최우선 호가",
        status=FactStatus.PASS,
        detail=(
            f"매수 {format_decimal(best_bid, currency)} · "
            f"매도 {format_decimal(best_ask, currency)} "
            f"{currency} · 스프레드 {spread_text}{percent_text}"
        ),
    )


def _reference_execution_price(
    intent: OrderIntent, quote: QuoteFactsContext
) -> tuple[Decimal | None, str, FactStatus]:
    if intent.order_type is OrderType.LIMIT:
        if intent.limit_price is None:
            raise PretradeFactsError("LIMIT 주문에는 지정가가 있어야 합니다.")
        price = _require_positive(intent.limit_price, "limit_price")
        return price, "지정가(LIMIT) 기준", FactStatus.PASS

    best_bid, best_ask = _best_bid_ask(quote.orderbook, intent.currency)
    book_usable = (
        quote.orderbook_fresh
        and best_bid is not None
        and best_ask is not None
        and best_bid <= best_ask
    )
    opposite = best_ask if intent.side is OrderSide.BUY else best_bid
    if book_usable and opposite is not None:
        return opposite, "MARKET 추정 · 반대편 최우선 호가(체결 보장 아님)", FactStatus.PASS

    last_trade = _optional_positive(quote.last_trade_price, "last_trade_price")
    if last_trade is not None:
        if quote.orderbook is None:
            fallback_reason = "호가 없음"
        elif quote.orderbook.currency != intent.currency:
            fallback_reason = "호가 통화 불일치"
        elif not quote.orderbook_fresh:
            fallback_reason = "호가 신선도 저하"
        elif best_bid is None or best_ask is None:
            fallback_reason = "호가 불완전"
        else:
            fallback_reason = "호가 역전"
        return (
            last_trade,
            f"MARKET 추정 · {fallback_reason}, 최근 체결가로 대체(낮은 신뢰도)",
            FactStatus.WARN,
        )
    return None, "기준가 정보 없음", FactStatus.UNAVAILABLE


def _reference_price_row(
    intent: OrderIntent, quote: QuoteFactsContext
) -> tuple[FactRow, Decimal | None]:
    price, basis, status = _reference_execution_price(intent, quote)
    if price is None:
        return (
            FactRow(
                key="reference_price",
                label="기준 체결가",
                status=status,
                detail=basis,
            ),
            None,
        )
    return (
        FactRow(
            key="reference_price",
            label="기준 체결가",
            status=status,
            detail=f"{format_decimal(price, intent.currency)} {intent.currency} · {basis}",
        ),
        price,
    )


def _estimated_notional_row(
    intent: OrderIntent, reference_price: Decimal | None
) -> tuple[FactRow, Decimal | None]:
    if reference_price is None:
        return (
            FactRow(
                key="estimated_notional",
                label="추정 주문 금액",
                status=FactStatus.UNAVAILABLE,
                detail="기준 체결가 없음으로 추정 불가",
            ),
            None,
        )
    notional = intent.quantity * reference_price
    suffix = (
        " · 체결 보장 아니며 MARKET 체결 금액 상한도 아님"
        if intent.order_type is OrderType.MARKET
        else " · 체결 보장 아님"
    )
    return (
        FactRow(
            key="estimated_notional",
            label="추정 주문 금액",
            status=FactStatus.PASS,
            detail=f"{format_decimal(notional, intent.currency)} {intent.currency}{suffix}",
        ),
        notional,
    )


def _buy_buying_power_row(
    intent: OrderIntent,
    display_notional: Decimal | None,
    mode: Literal["PAPER", "LIVE"],
) -> FactRow:
    power = _require_nonnegative(intent.cash_buying_power, "cash_buying_power")
    power_text = f"{format_decimal(power, intent.currency)} {intent.currency}"
    canonical_notional = RiskGate.estimate(intent)
    if canonical_notional > power:
        return FactRow(
            key="buying_power",
            label="매수가능금액",
            status=FactStatus.BLOCK,
            detail=f"캐노니컬 추정 금액이 매수가능금액 초과 · 매수가능 {power_text}",
        )
    if display_notional is None:
        return FactRow(
            key="buying_power",
            label="매수가능금액",
            status=FactStatus.UNAVAILABLE,
            detail=f"매수가능 {power_text} · 기준가 없음으로 비교 불가",
        )
    if display_notional > power:
        return FactRow(
            key="buying_power",
            label="매수가능금액",
            status=FactStatus.WARN,
            detail=(
                f"호가 기준 추정액이 매수가능금액을 넘을 수 있음 · 매수가능 {power_text} · "
                "Enter 후 fresh 계좌 재조회"
            ),
        )
    if mode == "LIVE":
        return FactRow(
            key="buying_power",
            label="매수가능금액",
            status=FactStatus.WARN,
            detail=f"PAPER 생성 시점 매수가능 {power_text} · Enter 후 fresh 계좌 재조회",
        )
    return FactRow(
        key="buying_power",
        label="매수가능금액",
        status=FactStatus.PASS,
        detail=f"매수가능 {power_text} · 추정 금액 이내",
    )


def _sellable_quantity(
    holding_quantity: Decimal, open_orders: tuple[OpenOrder, ...], symbol: str
) -> Decimal:
    """보유 수량에서 동일 종목 미체결 매도 주문 잔량을 뺀, 0 하한 보수적 추정치.

    ``find_open_order_duplicates``(캐노니컬 미체결 상태 판정)를 그대로 재사용한다
    -- 브로커 공식 매도가능수량이 아니라 이 클라이언트의 보수적 추정치다.
    """
    reserved = sum(
        (
            order.remaining_quantity
            for order in find_open_order_duplicates(open_orders, symbol, "SELL")
        ),
        Decimal("0"),
    )
    return max(holding_quantity - reserved, Decimal("0"))


def _sell_holding_row(
    intent: OrderIntent,
    open_orders: tuple[OpenOrder, ...] | None,
    mode: Literal["PAPER", "LIVE"],
) -> FactRow:
    holding = _require_nonnegative(intent.holding_quantity, "holding_quantity")
    holding_text = canonical_decimal_text(holding)
    if intent.quantity > holding:
        return FactRow(
            key="sellable_quantity",
            label="보유/매도가능 수량",
            status=FactStatus.BLOCK,
            detail=f"매도 수량이 보유 수량({holding_text}) 초과",
        )
    if open_orders is None:
        return FactRow(
            key="sellable_quantity",
            label="보유/매도가능 수량",
            status=FactStatus.UNAVAILABLE,
            detail=f"보유 {holding_text} · 미체결 주문 정보 없음으로 매도가능(추정) 계산 불가",
        )
    sellable = _sellable_quantity(holding, open_orders, intent.symbol)
    sellable_text = canonical_decimal_text(sellable)
    if intent.quantity > sellable:
        return FactRow(
            key="sellable_quantity",
            label="보유/매도가능 수량",
            status=FactStatus.WARN,
            detail=(
                f"보유 {holding_text} · 매도가능(추정, 공식 수치 아님) {sellable_text} · "
                "동일 종목 미체결 매도 주문 반영 시 수량 부족 가능 · Enter 후 fresh 재조회"
            ),
        )
    if mode == "LIVE":
        return FactRow(
            key="sellable_quantity",
            label="보유/매도가능 수량",
            status=FactStatus.WARN,
            detail=(
                f"마지막 확인값 · 보유 {holding_text} · "
                f"매도가능(추정, 공식 수치 아님) {sellable_text} · Enter 후 fresh 재조회"
            ),
        )
    return FactRow(
        key="sellable_quantity",
        label="보유/매도가능 수량",
        status=FactStatus.PASS,
        detail=f"보유 {holding_text} · 매도가능(추정, 공식 수치 아님) {sellable_text}",
    )


def _duplicate_orders_row(
    intent: OrderIntent,
    open_orders: tuple[OpenOrder, ...] | None,
    mode: Literal["PAPER", "LIVE"],
) -> FactRow:
    if open_orders is None:
        return FactRow(
            key="duplicate_orders",
            label="동일 종목·방향 미체결 주문",
            status=FactStatus.UNAVAILABLE,
            detail="미체결 주문 정보 없음",
        )
    duplicates = find_open_order_duplicates(open_orders, intent.symbol, intent.side.value)
    if duplicates:
        status = FactStatus.BLOCK if mode == "LIVE" else FactStatus.WARN
        detail = (
            f"마지막 확인값 기준 {len(duplicates)}건 존재 · Enter 후 재조회에서도 차단 대상"
            if mode == "LIVE"
            else f"{len(duplicates)}건 존재 · PAPER 확인 가능, LIVE 제출은 차단됨"
        )
        return FactRow(
            key="duplicate_orders",
            label="동일 종목·방향 미체결 주문",
            status=status,
            detail=detail,
        )
    if mode == "LIVE":
        return FactRow(
            key="duplicate_orders",
            label="동일 종목·방향 미체결 주문",
            status=FactStatus.WARN,
            detail="마지막 확인값 기준 없음 · Enter 후 fresh GET 재조회",
        )
    return FactRow(
        key="duplicate_orders",
        label="동일 종목·방향 미체결 주문",
        status=FactStatus.PASS,
        detail="없음",
    )


def build_pretrade_facts(
    intent: OrderIntent,
    quote: QuoteFactsContext,
    *,
    now: datetime,
    open_orders: tuple[OpenOrder, ...] | None,
    mode: Literal["PAPER", "LIVE"] = "PAPER",
) -> PretradeFacts:
    """이미 검증된 캐노니컬 입력만으로 사전 점검 사실을 만든다.

    ``intent``는 ``OrderPreview``/``LiveOrderPlan``이 이미 만든 불변 주문 의도이고,
    ``quote``는 앱이 이미 메모리에 들고 있는 시세/호가창 상태이며, ``open_orders``는
    이미 로드된 미체결 주문 목록(없으면 ``None``)이다. 이 함수는 아무 것도 조회하지
    않고, 새로운 차단 임계값도 만들지 않는다. 잘못된 입력(비유한/음수/naive
    datetime 등)은 :class:`PretradeFactsError`로 fail-closed 거부된다.
    """
    if not isinstance(intent, OrderIntent):
        raise PretradeFactsError("intent 값은 OrderIntent여야 합니다.")
    if not isinstance(quote, QuoteFactsContext):
        raise PretradeFactsError("quote 값은 QuoteFactsContext여야 합니다.")
    if mode not in {"PAPER", "LIVE"}:
        raise PretradeFactsError("mode 값은 PAPER 또는 LIVE여야 합니다.")
    if not isinstance(intent.side, OrderSide):
        raise PretradeFactsError("side 값은 OrderSide여야 합니다.")
    if not isinstance(intent.order_type, OrderType):
        raise PretradeFactsError("order_type 값은 OrderType이어야 합니다.")
    if not isinstance(intent.symbol, str) or not intent.symbol.strip():
        raise PretradeFactsError("symbol 값은 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(intent.currency, str) or not intent.currency.strip():
        raise PretradeFactsError("currency 값은 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(quote.quote_fresh, bool):
        raise PretradeFactsError("quote_fresh 값은 bool이어야 합니다.")
    if not isinstance(quote.orderbook_fresh, bool):
        raise PretradeFactsError("orderbook_fresh 값은 bool이어야 합니다.")
    now = _require_aware(now, "now")
    _require_positive(intent.quantity, "quantity")
    _require_positive(intent.reference_last_price, "reference_last_price")
    _require_nonnegative(intent.holding_quantity, "holding_quantity")
    _require_nonnegative(intent.cash_buying_power, "cash_buying_power")
    if intent.order_type is OrderType.LIMIT:
        if intent.limit_price is None:
            raise PretradeFactsError("LIMIT 주문에는 지정가가 있어야 합니다.")
        _require_positive(intent.limit_price, "limit_price")
    elif intent.limit_price is not None:
        raise PretradeFactsError("MARKET 주문에는 지정가가 없어야 합니다.")

    quote_row = _quote_freshness_row(quote, now)
    best_quote_row = _best_quote_row(
        quote.orderbook,
        intent.currency,
        orderbook_fresh=quote.orderbook_fresh,
        now=now,
    )
    if mode == "LIVE":
        quote_row = replace(quote_row, detail=f"마지막 확인값 · {quote_row.detail}")
        best_quote_row = replace(
            best_quote_row,
            detail=f"마지막 확인값 · {best_quote_row.detail}",
        )
    reference_row, reference_price = _reference_price_row(intent, quote)
    notional_row, estimated_notional = _estimated_notional_row(intent, reference_price)
    if intent.side is OrderSide.BUY:
        account_row = _buy_buying_power_row(intent, estimated_notional, mode)
    else:
        account_row = _sell_holding_row(intent, open_orders, mode)
    duplicate_row = _duplicate_orders_row(intent, open_orders, mode)

    return PretradeFacts(
        symbol=intent.symbol,
        mode=mode,
        side=intent.side,
        order_type=intent.order_type,
        as_of=now,
        rows=(
            quote_row,
            best_quote_row,
            reference_row,
            notional_row,
            account_row,
            duplicate_row,
        ),
    )
