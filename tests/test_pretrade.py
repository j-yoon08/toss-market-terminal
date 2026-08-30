from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from toss_market_terminal.models import OpenOrder, Orderbook, OrderbookEntry
from toss_market_terminal.order_preview import OrderSide, OrderType, build_preview
from toss_market_terminal.pretrade import (
    FactStatus,
    PretradeFactsError,
    QuoteFactsContext,
    build_pretrade_facts,
)

NOW = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)


def preview(
    *,
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "1",
    reference: str = "40",
    limit: str | None = None,
    holding: str = "5",
    power: str = "500",
):
    return build_preview(
        account_no="*******8901",
        account_seq=1,
        symbol="AAPL",
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit,
        reference_last_price=reference,
        holding_quantity=holding,
        cash_buying_power=power,
        market="us",
        currency="USD",
    )


def book(*, bid: str = "39", ask: str = "41", currency: str = "USD") -> Orderbook:
    return Orderbook(
        currency=currency,
        bids=(OrderbookEntry(Decimal(bid), Decimal("10")),),
        asks=(OrderbookEntry(Decimal(ask), Decimal("12")),),
        timestamp="2026-08-30T02:59:58Z",
    )


def quote(
    *,
    orderbook: Orderbook | None = None,
    last: Decimal | None = Decimal("40"),
    timestamp: str | None = "2026-08-30T02:59:58Z",
    fresh: bool = True,
    book_fresh: bool = True,
) -> QuoteFactsContext:
    return QuoteFactsContext(
        orderbook=orderbook,
        last_trade_price=last,
        quote_timestamp=timestamp,
        quote_fresh=fresh,
        orderbook_fresh=book_fresh,
    )


def open_order(*, side: str = "BUY", remaining: str = "1") -> OpenOrder:
    amount = Decimal(remaining)
    return OpenOrder(
        order_id="secret-broker-order-id",
        symbol="AAPL",
        side=side,
        order_type="LIMIT",
        status="PENDING",
        quantity=amount,
        price=Decimal("40"),
        filled_quantity=Decimal("0"),
        remaining_quantity=amount,
    )


def facts(
    *,
    order_preview=None,
    quote_context: QuoteFactsContext | None = None,
    orders: tuple[OpenOrder, ...] | None = (),
    mode: str = "PAPER",
    now: datetime = NOW,
):
    return build_pretrade_facts(
        (order_preview or preview()).intent,
        quote_context or quote(orderbook=book()),
        now=now,
        open_orders=orders,
        mode=mode,  # type: ignore[arg-type]
    )


def test_market_buy_uses_opposite_best_quote_and_decimal_notional() -> None:
    result = facts()
    reference = result.row("reference_price")
    notional = result.row("estimated_notional")
    spread = result.row("best_quote")

    assert result.mode == "PAPER"
    assert reference is not None and reference.status is FactStatus.PASS
    assert "41 USD" in reference.detail
    assert "반대편 최우선 호가" in reference.detail
    assert notional is not None and "41 USD" in notional.detail
    assert "상한도 아님" in notional.detail
    assert spread is not None and "스프레드 2" in spread.detail


def test_market_sell_uses_best_bid() -> None:
    result = facts(order_preview=preview(side=OrderSide.SELL))
    assert "39 USD" in result.row("reference_price").detail  # type: ignore[union-attr]


def test_limit_uses_limit_price_even_without_orderbook() -> None:
    result = facts(
        order_preview=preview(order_type=OrderType.LIMIT, limit="42"),
        quote_context=quote(orderbook=None),
    )
    assert "42 USD" in result.row("reference_price").detail  # type: ignore[union-attr]
    assert result.row("best_quote").status is FactStatus.UNAVAILABLE  # type: ignore[union-attr]


def test_market_falls_back_to_last_trade_as_warning() -> None:
    result = facts(quote_context=quote(orderbook=None, last=Decimal("40")))
    reference = result.row("reference_price")
    assert reference is not None and reference.status is FactStatus.WARN
    assert "최근 체결가로 대체" in reference.detail


def test_market_without_book_or_last_trade_is_unavailable_not_zero() -> None:
    result = facts(quote_context=quote(orderbook=None, last=None))
    assert result.row("reference_price").status is FactStatus.UNAVAILABLE  # type: ignore[union-attr]
    assert result.row("estimated_notional").status is FactStatus.UNAVAILABLE  # type: ignore[union-attr]
    assert " 0 " not in "\n".join(result.to_privacy_safe_lines())


def test_quote_age_uses_aware_timezone_and_injected_clock() -> None:
    kst = timezone(timedelta(hours=9))
    result = facts(
        quote_context=quote(timestamp="2026-08-30T11:59:58+09:00"),
        now=datetime(2026, 8, 30, 12, 0, tzinfo=kst),
    )
    row = result.row("quote_freshness")
    assert row is not None and row.status is FactStatus.PASS
    assert "2.0초" in row.detail


def test_canonical_stale_truth_is_block_without_new_threshold() -> None:
    result = facts(quote_context=quote(fresh=False))
    row = result.row("quote_freshness")
    assert row is not None and row.status is FactStatus.BLOCK
    assert result.has_block is True


def test_market_book_estimate_over_power_warns_but_does_not_create_new_block() -> None:
    safe_preview = preview(reference="40", power="50")
    result = facts(
        order_preview=safe_preview,
        quote_context=quote(orderbook=book(bid="149", ask="150")),
    )
    row = result.row("buying_power")
    assert row is not None and row.status is FactStatus.WARN
    assert "Enter 후 fresh 계좌 재조회" in row.detail
    assert result.has_block is False


def test_stale_orderbook_is_unavailable_and_market_falls_back_to_last_trade() -> None:
    result = facts(
        quote_context=quote(
            orderbook=book(bid="39", ask="41"),
            last=Decimal("40"),
            book_fresh=False,
        )
    )
    best_quote = result.row("best_quote")
    reference = result.row("reference_price")
    assert best_quote is not None and best_quote.status is FactStatus.UNAVAILABLE
    assert "MARKET 기준가로 사용하지 않음" in best_quote.detail
    assert reference is not None and reference.status is FactStatus.WARN
    assert "40 USD" in reference.detail
    assert "최근 체결가로 대체" in reference.detail


def test_crossed_orderbook_is_warning_and_not_used_as_market_reference() -> None:
    result = facts(
        quote_context=quote(
            orderbook=book(bid="42", ask="41"),
            last=Decimal("40"),
        )
    )
    best_quote = result.row("best_quote")
    reference = result.row("reference_price")
    assert best_quote is not None and best_quote.status is FactStatus.WARN
    assert "호가 역전" in best_quote.detail
    assert reference is not None and reference.status is FactStatus.WARN
    assert "40 USD" in reference.detail


def test_live_account_and_empty_duplicate_rows_are_last_known_warnings() -> None:
    result = facts(orders=(), mode="LIVE")
    quote_freshness = result.row("quote_freshness")
    best_quote = result.row("best_quote")
    buying_power = result.row("buying_power")
    duplicates = result.row("duplicate_orders")
    assert quote_freshness is not None and "마지막 확인값" in quote_freshness.detail
    assert best_quote is not None and "마지막 확인값" in best_quote.detail
    assert buying_power is not None and buying_power.status is FactStatus.WARN
    assert duplicates is not None and duplicates.status is FactStatus.WARN
    assert "PAPER 생성 시점" in buying_power.detail
    assert "Enter 후 fresh" in buying_power.detail
    assert "마지막 확인값 기준 없음" in duplicates.detail


def test_paper_duplicate_warns_and_live_duplicate_blocks() -> None:
    orders = (open_order(),)
    paper = facts(orders=orders, mode="PAPER")
    live = facts(orders=orders, mode="LIVE")
    assert paper.row("duplicate_orders").status is FactStatus.WARN  # type: ignore[union-attr]
    assert live.row("duplicate_orders").status is FactStatus.BLOCK  # type: ignore[union-attr]
    assert "PAPER 확인 가능" in paper.row("duplicate_orders").detail  # type: ignore[union-attr]


def test_missing_open_orders_is_unavailable_not_assumed_empty() -> None:
    result = facts(orders=None)
    assert result.row("duplicate_orders").status is FactStatus.UNAVAILABLE  # type: ignore[union-attr]


def test_sellable_quantity_is_conservative_and_explicitly_not_official() -> None:
    result = facts(
        order_preview=preview(side=OrderSide.SELL, quantity="2", holding="2"),
        orders=(open_order(side="SELL", remaining="1"),),
    )
    row = result.row("sellable_quantity")
    assert row is not None and row.status is FactStatus.WARN
    assert "매도가능(추정, 공식 수치 아님) 1" in row.detail


def test_orderbook_currency_mismatch_is_unavailable_and_not_used_as_reference() -> None:
    result = facts(quote_context=quote(orderbook=book(currency="KRW")))
    assert result.row("best_quote").status is FactStatus.UNAVAILABLE  # type: ignore[union-attr]
    reference = result.row("reference_price")
    assert reference is not None and reference.status is FactStatus.WARN
    assert "최근 체결가" in reference.detail


@pytest.mark.parametrize(
    ("quote_context", "now"),
    [
        (quote(last=Decimal("NaN")), NOW),
        (quote(timestamp="not-a-timestamp"), NOW),
        (quote(), datetime(2026, 8, 30, 3, 0)),
        (quote(orderbook=book(bid="-1")), NOW),
    ],
)
def test_malformed_nonfinite_negative_or_naive_inputs_fail_closed(
    quote_context: QuoteFactsContext, now: datetime
) -> None:
    with pytest.raises(PretradeFactsError):
        facts(quote_context=quote_context, now=now)


def test_invalid_mode_fails_closed() -> None:
    with pytest.raises(PretradeFactsError):
        facts(mode="AUTO")


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "BUY"},
        {"order_type": "MARKET"},
        {"symbol": ""},
        {"currency": ""},
        {"quantity": Decimal("NaN")},
        {"reference_last_price": Decimal("NaN")},
        {"holding_quantity": Decimal("-1")},
        {"cash_buying_power": Decimal("-1")},
        {"limit_price": Decimal("1")},
    ],
)
def test_malformed_order_intent_fails_closed(changes: dict[str, object]) -> None:
    malformed = replace(preview().intent, **changes)
    with pytest.raises(PretradeFactsError):
        build_pretrade_facts(
            malformed,
            quote(orderbook=book()),
            now=NOW,
            open_orders=(),
        )


def test_non_bool_quote_freshness_fails_closed() -> None:
    malformed = replace(quote(orderbook=book()), quote_fresh=1)
    with pytest.raises(PretradeFactsError):
        build_pretrade_facts(preview().intent, malformed, now=NOW, open_orders=())


def test_non_bool_orderbook_freshness_fails_closed() -> None:
    malformed = replace(quote(orderbook=book()), orderbook_fresh=1)
    with pytest.raises(PretradeFactsError):
        build_pretrade_facts(preview().intent, malformed, now=NOW, open_orders=())


def test_privacy_safe_lines_never_include_account_token_order_id_or_fingerprint() -> None:
    order_preview = preview()
    result = facts(order_preview=order_preview, orders=(open_order(),), mode="LIVE")
    rendered = "\n".join(result.to_privacy_safe_lines())
    forbidden = (
        "50123456701",
        "*******8901",
        "account_seq",
        "secret-broker-order-id",
        "token",
        order_preview.fingerprint,
        order_preview.approval_phrase,
    )
    assert all(value not in rendered for value in forbidden)


def test_models_are_immutable() -> None:
    result = facts()
    with pytest.raises((AttributeError, TypeError)):
        result.mode = "LIVE"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        replace(result.rows[0], detail="changed").detail = "again"  # type: ignore[misc]
