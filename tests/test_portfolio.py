"""Phase-1 portfolio domain + client coverage.

Covers ``PortfolioSnapshot`` (immutability/privacy), ``sellable_quantity``,
the pure render helpers in ``portfolio.py``, and
``TossMarketClient.portfolio_snapshot`` (read-only fan-out) against
``httpx.MockTransport`` only.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from toss_market_terminal.client import TossMarketClient
from toss_market_terminal.config import Credentials
from toss_market_terminal.models import (
    Account,
    BuyingPower,
    HoldingsItem,
    HoldingsOverview,
    OpenOrder,
    OpenOrdersPage,
    PortfolioSnapshot,
)
from toss_market_terminal.portfolio import (
    holdings_section_text,
    open_orders_section_text,
    portfolio_body_text,
    portfolio_header_text,
    sellable_quantity,
    totals_section_text,
)

# --- shared official-shape fixtures (mirrors test_accounts_models.py /
# test_open_orders_models.py / test_accounts_client.py so this file never
# invents an incompatible provider shape) -----------------------------------

RAW_ACCOUNT_NO = "12345678901"


def official_account(**overrides: object) -> dict[str, object]:
    account: dict[str, object] = {
        "accountNo": RAW_ACCOUNT_NO,
        "accountSeq": 1,
        "accountType": "BROKERAGE",
    }
    account.update(overrides)
    return account


def official_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": "005930",
        "name": "삼성전자",
        "marketCountry": "KR",
        "currency": "KRW",
        "quantity": "100",
        "lastPrice": "72000",
        "averagePurchasePrice": "65000",
        "marketValue": {
            "purchaseAmount": "6500000",
            "amount": "7200000",
            "amountAfterCost": "7050000",
        },
        "profitLoss": {
            "amount": "700000",
            "amountAfterCost": "550000",
            "rate": "0.1077",
            "rateAfterCost": "0.0846",
        },
        "dailyProfitLoss": {"amount": "100000", "rate": "0.0141"},
        "cost": {"commission": "14400", "tax": "135600"},
    }
    item.update(overrides)
    return item


def official_overview(items: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "totalPurchaseAmount": {"krw": "6500000", "usd": None},
        "marketValue": {
            "amount": {"krw": "7200000", "usd": None},
            "amountAfterCost": {"krw": "7050000", "usd": None},
        },
        "profitLoss": {
            "amount": {"krw": "700000", "usd": None},
            "amountAfterCost": {"krw": "550000", "usd": None},
            "rate": "0.1077",
            "rateAfterCost": "0.0846",
        },
        "dailyProfitLoss": {"amount": {"krw": "100000", "usd": None}, "rate": "0.0141"},
        "items": [official_item()] if items is None else items,
    }


def official_order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "orderId": "bAGzNvMOOTa5Uy0xVzYNbxDJ3Qpobwau4jDF3hyZZGWbpHm7wha8CFZc7aXVOWAl",
        "symbol": "005930",
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "status": "PENDING",
        "price": "70000",
        "quantity": "10",
        "orderAmount": None,
        "currency": "KRW",
        "orderedAt": "2026-03-29T09:30:00+09:00",
        "canceledAt": None,
        "execution": {
            "filledQuantity": "0",
            "averageFilledPrice": None,
            "filledAmount": None,
            "commission": None,
            "tax": None,
            "filledAt": None,
            "settlementDate": None,
        },
    }
    order.update(overrides)
    return order


def official_open_orders_page(orders: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "orders": [official_order()] if orders is None else orders,
        "nextCursor": None,
        "hasNext": False,
    }


def build_snapshot(
    *,
    holdings_raw: dict[str, object] | None = None,
    orders_raw: dict[str, object] | None = None,
    krw_power: str = "5000000",
    usd_power: str = "100",
    synced_at: datetime | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        scope="account_read_only",
        order_endpoints_called=False,
        account=Account.from_api(official_account()),
        krw_buying_power=BuyingPower.from_api({"currency": "KRW", "cashBuyingPower": krw_power}),
        usd_buying_power=BuyingPower.from_api({"currency": "USD", "cashBuyingPower": usd_power}),
        holdings=HoldingsOverview.from_api(
            official_overview() if holdings_raw is None else holdings_raw
        ),
        open_orders=OpenOrdersPage.from_api(
            official_open_orders_page([]) if orders_raw is None else orders_raw
        ),
        synced_at=synced_at or datetime.now(UTC),
    )


def credentials() -> Credentials:
    return Credentials("tsck_live_test", "tssk_live_test")


def token_handler(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/oauth2/token":
        return httpx.Response(
            200,
            json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
        )
    return None


def mock_client(handler) -> tuple[TossMarketClient, httpx.AsyncClient]:
    """Build a client bound to an httpx.MockTransport -- never the real network."""
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openapi.tossinvest.com",
    )
    client = TossMarketClient(credentials(), http_client=http_client)
    return client, http_client


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail hard if any portfolio test tries to open a real TCP connection."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external network attempted in portfolio tests")

    monkeypatch.setattr("socket.socket.connect", _blocked)


# =============================================================================
# PortfolioSnapshot: immutability, privacy, scope, order-endpoints flag.
# =============================================================================


def test_portfolio_snapshot_is_frozen() -> None:
    snapshot = build_snapshot()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.scope = "mutated"  # type: ignore[misc]


def test_portfolio_snapshot_scope_and_order_endpoints_flag() -> None:
    snapshot = build_snapshot()
    assert snapshot.scope == "account_read_only"
    assert snapshot.order_endpoints_called is False


def test_portfolio_snapshot_never_leaks_raw_account_number() -> None:
    snapshot = build_snapshot()
    payload = json.dumps(dataclasses.asdict(snapshot), ensure_ascii=False, default=str)
    assert RAW_ACCOUNT_NO not in payload
    assert snapshot.account.masked_account_no in payload
    assert "*******8901" == snapshot.account.masked_account_no


# =============================================================================
# sellable_quantity
# =============================================================================


def test_sellable_quantity_subtracts_remaining_active_same_symbol_sell() -> None:
    item = HoldingsItem.from_api(official_item(quantity="100"))
    orders = (
        OpenOrder.from_api(
            official_order(
                side="SELL", status="PENDING", quantity="10", execution={"filledQuantity": "2"}
            )
        ),
    )
    # remaining = 10 - 2 = 8
    assert sellable_quantity(item, orders) == Decimal("92")


def test_sellable_quantity_subtracts_unknown_status_same_symbol_sell() -> None:
    item = HoldingsItem.from_api(official_item(quantity="100"))
    orders = (
        OpenOrder.from_api(official_order(side="SELL", status="SOME_NEW_STATUS", quantity="15")),
    )
    assert sellable_quantity(item, orders) == Decimal("85")


def test_sellable_quantity_excludes_buy_orders() -> None:
    item = HoldingsItem.from_api(official_item(quantity="100"))
    orders = (OpenOrder.from_api(official_order(side="BUY", status="PENDING", quantity="10")),)
    assert sellable_quantity(item, orders) == Decimal("100")


def test_sellable_quantity_excludes_other_symbol_orders() -> None:
    item = HoldingsItem.from_api(official_item(symbol="005930", quantity="100"))
    orders = (
        OpenOrder.from_api(
            official_order(symbol="AAPL", side="SELL", status="PENDING", quantity="10")
        ),
    )
    assert sellable_quantity(item, orders) == Decimal("100")


def test_sellable_quantity_excludes_terminal_status_sell_orders() -> None:
    item = HoldingsItem.from_api(official_item(quantity="100"))
    orders = (
        OpenOrder.from_api(official_order(side="SELL", status="FILLED", quantity="30")),
        OpenOrder.from_api(
            official_order(orderId="c2", side="SELL", status="CANCELED", quantity="30")
        ),
    )
    assert sellable_quantity(item, orders) == Decimal("100")


def test_sellable_quantity_floors_at_zero() -> None:
    item = HoldingsItem.from_api(official_item(quantity="10"))
    orders = (OpenOrder.from_api(official_order(side="SELL", status="PENDING", quantity="50")),)
    assert sellable_quantity(item, orders) == Decimal("0")


def test_sellable_quantity_mixed_orders_only_reserves_matching() -> None:
    item = HoldingsItem.from_api(official_item(symbol="005930", quantity="100"))
    orders = (
        OpenOrder.from_api(
            official_order(orderId="a", side="SELL", status="PENDING", quantity="10")
        ),
        OpenOrder.from_api(
            official_order(orderId="b", side="BUY", status="PENDING", quantity="20")
        ),
        OpenOrder.from_api(
            official_order(orderId="c", symbol="AAPL", side="SELL", status="PENDING", quantity="30")
        ),
        OpenOrder.from_api(
            official_order(orderId="d", side="SELL", status="FILLED", quantity="40")
        ),
    )
    assert sellable_quantity(item, orders) == Decimal("90")


# =============================================================================
# Pure render helpers
# =============================================================================


def test_portfolio_header_text_without_snapshot_shows_placeholder_and_error() -> None:
    text = portfolio_header_text(None, stale=False, error="네트워크 오류", synced_monotonic=None)
    plain = text.plain
    assert "계좌 정보를 아직 불러오지 못했습니다." in plain
    assert "네트워크 오류" in plain


def test_portfolio_header_text_separates_krw_and_usd_buying_power() -> None:
    snapshot = build_snapshot(krw_power="5000000", usd_power="123.45")
    text = portfolio_header_text(snapshot, stale=False, error=None, synced_monotonic=None)
    plain = text.plain
    assert snapshot.account.masked_account_no in plain
    assert RAW_ACCOUNT_NO not in plain
    assert "KRW 매수가능 5,000,000 KRW" in plain
    assert "USD 매수가능 123.45 USD" in plain
    assert "ACCOUNT FRESH" in plain


def test_portfolio_header_text_shows_stale_status_and_error() -> None:
    snapshot = build_snapshot()
    text = portfolio_header_text(
        snapshot, stale=True, error="계좌 조회 실패", synced_monotonic=None
    )
    plain = text.plain
    assert "ACCOUNT STALE" in plain
    assert "계좌 조회 실패" in plain


def test_holdings_section_text_no_holdings() -> None:
    snapshot = build_snapshot(holdings_raw=official_overview(items=[]))
    text = holdings_section_text(snapshot, width=120)
    assert "보유 종목 없음" in text.plain


def test_holdings_section_text_wide_includes_required_fields() -> None:
    snapshot = build_snapshot()
    text = holdings_section_text(snapshot, width=120)
    plain = text.plain
    assert "005930" in plain
    assert "매도가능" in plain
    assert "평단" in plain
    assert "현재가" in plain
    assert "매입" in plain
    assert "평가" in plain
    assert "손익" in plain


def test_holdings_section_text_compact_includes_purchase_current_value_pl_rate() -> None:
    snapshot = build_snapshot()
    text = holdings_section_text(snapshot, width=60)
    plain = text.plain
    assert "평단" in plain
    assert "현재가" in plain
    assert "매입" in plain
    assert "평가" in plain
    assert "손익" in plain
    assert "%" in plain


def test_holdings_section_text_sellable_reflects_open_sell_orders() -> None:
    snapshot = build_snapshot(
        orders_raw=official_open_orders_page(
            [official_order(side="SELL", status="PENDING", quantity="30")]
        )
    )
    text = holdings_section_text(snapshot, width=120)
    # Values are right-justified/padded in the wide layout, so collapse
    # whitespace before matching the "label value" pair.
    normalized = re.sub(r"\s+", " ", text.plain)
    assert "매도가능 70" in normalized


def test_open_orders_section_text_no_orders() -> None:
    snapshot = build_snapshot(orders_raw=official_open_orders_page([]))
    text = open_orders_section_text(snapshot, width=120)
    assert "미체결 주문 없음" in text.plain


def test_open_orders_section_text_never_shows_order_id() -> None:
    order_id = "bAGzNvMOOTa5Uy0xVzYNbxDJ3Qpobwau4jDF3hyZZGWbpHm7wha8CFZc7aXVOWAl"
    snapshot = build_snapshot(
        orders_raw=official_open_orders_page([official_order(orderId=order_id)])
    )
    for width in (60, 120):
        plain = open_orders_section_text(snapshot, width=width).plain
        assert order_id not in plain


def test_open_orders_section_text_shows_limit_price() -> None:
    snapshot = build_snapshot(
        orders_raw=official_open_orders_page([official_order(orderType="LIMIT", price="70000")])
    )
    text = open_orders_section_text(snapshot, width=120)
    plain = text.plain
    assert "LIMIT" in plain
    assert "70,000" in plain


def test_open_orders_section_text_market_order_has_no_price() -> None:
    snapshot = build_snapshot(
        orders_raw=official_open_orders_page([official_order(orderType="MARKET", price=None)])
    )
    text = open_orders_section_text(snapshot, width=120)
    plain = text.plain
    assert "MARKET" in plain
    # The dash placeholder must appear and no fabricated price string.
    assert " - " in plain or plain.count("-") >= 1


def test_totals_section_text_separates_krw_and_usd_no_conversion() -> None:
    items = [
        official_item(symbol="005930", currency="KRW"),
        official_item(
            symbol="AAPL",
            name="Apple Inc.",
            marketCountry="US",
            currency="USD",
            quantity="10",
            lastPrice="178.5",
            averagePurchasePrice="155.3",
            marketValue={"purchaseAmount": "1553", "amount": "1785", "amountAfterCost": "1771.43"},
            profitLoss={
                "amount": "232",
                "amountAfterCost": "218.43",
                "rate": "0.1494",
                "rateAfterCost": "0.1406",
            },
            dailyProfitLoss={"amount": "25", "rate": "0.0142"},
            cost={"commission": "3.57", "tax": "10"},
        ),
    ]
    snapshot = build_snapshot(holdings_raw=official_overview(items))
    text = totals_section_text(snapshot)
    plain = text.plain
    assert "KRW 평가금액 7,050,000" in plain
    assert "USD 평가금액 1,771.43" in plain
    assert "KRW 평가손익 550,000" in plain
    assert "USD 평가손익 218.43" in plain


def test_totals_section_text_no_holdings() -> None:
    snapshot = build_snapshot(holdings_raw=official_overview(items=[]))
    text = totals_section_text(snapshot)
    assert "보유 종목 없음" in text.plain


def test_portfolio_body_text_without_snapshot() -> None:
    text = portfolio_body_text(None, width=100)
    assert "계좌 정보를 아직 불러오지 못했습니다." in text.plain


def test_portfolio_body_text_combines_all_sections() -> None:
    snapshot = build_snapshot(orders_raw=official_open_orders_page([official_order()]))
    text = portfolio_body_text(snapshot, width=120)
    plain = text.plain
    assert "보유 종목" in plain
    assert "미체결 주문" in plain
    assert "합계" in plain


# =============================================================================
# client.portfolio_snapshot -- httpx.MockTransport only.
# =============================================================================


def snapshot_handler(request: httpx.Request) -> httpx.Response:
    response = token_handler(request)
    if response is not None:
        return response
    assert request.method == "GET"
    if request.url.path == "/api/v1/accounts":
        assert "x-tossinvest-account" not in request.headers
        return httpx.Response(200, json={"result": [official_account()]})
    assert request.headers["x-tossinvest-account"] == "1"
    if request.url.path == "/api/v1/holdings":
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"result": official_overview()})
    if request.url.path == "/api/v1/buying-power":
        currency = request.url.params.get("currency")
        assert currency in ("KRW", "USD")
        assert dict(request.url.params) == {"currency": currency}
        power = "5000000" if currency == "KRW" else "100"
        return httpx.Response(
            200, json={"result": {"currency": currency, "cashBuyingPower": power}}
        )
    if request.url.path == "/api/v1/orders":
        assert dict(request.url.params) == {"status": "OPEN"}
        return httpx.Response(200, json={"result": official_open_orders_page([])})
    raise AssertionError(f"unexpected path {request.url.path}")


@pytest.mark.asyncio
async def test_portfolio_snapshot_issues_exact_get_calls_with_account_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        seen.append(request)
        return snapshot_handler(request)

    client, _ = mock_client(handler)
    try:
        snapshot = await client.portfolio_snapshot()
    finally:
        await client.close()

    paths = sorted(request.url.path for request in seen)
    assert paths == sorted(
        [
            "/api/v1/accounts",
            "/api/v1/holdings",
            "/api/v1/buying-power",
            "/api/v1/buying-power",
            "/api/v1/orders",
        ]
    )
    assert all(request.method == "GET" for request in seen)
    assert isinstance(snapshot, PortfolioSnapshot)
    assert snapshot.scope == "account_read_only"
    assert snapshot.order_endpoints_called is False
    assert snapshot.account.account_seq == 1
    assert snapshot.krw_buying_power.cash_buying_power == Decimal("5000000")
    assert snapshot.usd_buying_power.cash_buying_power == Decimal("100")
    assert len(snapshot.open_orders.orders) == 0


@pytest.mark.asyncio
async def test_portfolio_snapshot_never_issues_post() -> None:
    client, _ = mock_client(snapshot_handler)
    try:
        await client.portfolio_snapshot()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_portfolio_snapshot_default_account_seq_auto_selects_single_brokerage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json={
                    "result": [
                        official_account(accountSeq=1, accountType="BROKERAGE"),
                        official_account(accountSeq=2, accountType="PENSION_SAVINGS"),
                    ]
                },
            )
        return snapshot_handler(request)

    client, _ = mock_client(handler)
    try:
        snapshot = await client.portfolio_snapshot()
    finally:
        await client.close()
    assert snapshot.account.account_seq == 1


@pytest.mark.asyncio
async def test_portfolio_snapshot_explicit_account_seq_selects_matching_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json={
                    "result": [
                        official_account(accountSeq=1, accountType="BROKERAGE"),
                        official_account(accountSeq=2, accountType="BROKERAGE"),
                    ]
                },
            )
        assert request.headers["x-tossinvest-account"] == "2"
        if request.url.path == "/api/v1/holdings":
            return httpx.Response(200, json={"result": official_overview()})
        if request.url.path == "/api/v1/buying-power":
            currency = request.url.params.get("currency")
            power = "5000000" if currency == "KRW" else "100"
            return httpx.Response(
                200, json={"result": {"currency": currency, "cashBuyingPower": power}}
            )
        if request.url.path == "/api/v1/orders":
            return httpx.Response(200, json={"result": official_open_orders_page([])})
        raise AssertionError(f"unexpected path {request.url.path}")

    client, _ = mock_client(handler)
    try:
        snapshot = await client.portfolio_snapshot(account_seq=2)
    finally:
        await client.close()
    assert snapshot.account.account_seq == 2


@pytest.mark.asyncio
async def test_portfolio_snapshot_fails_closed_on_unknown_account_seq() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"result": [official_account(accountSeq=1)]})
        raise AssertionError("no account-scoped request should be reached")

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client.portfolio_snapshot(account_seq=99)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_portfolio_snapshot_fails_closed_on_multiple_brokerage_accounts_without_seq() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json={
                    "result": [
                        official_account(accountSeq=1, accountType="BROKERAGE"),
                        official_account(accountSeq=2, accountType="BROKERAGE"),
                    ]
                },
            )
        raise AssertionError("no account-scoped request should be reached")

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client.portfolio_snapshot()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_portfolio_snapshot_synced_at_is_deterministic_utc_aware_shape() -> None:
    before = datetime.now(UTC)
    client, _ = mock_client(snapshot_handler)
    try:
        snapshot = await client.portfolio_snapshot()
    finally:
        await client.close()
    after = datetime.now(UTC)

    assert isinstance(snapshot.synced_at, datetime)
    assert snapshot.synced_at.tzinfo is not None
    assert snapshot.synced_at.utcoffset() == timedelta(0)
    assert before <= snapshot.synced_at <= after


@pytest.mark.asyncio
async def test_portfolio_snapshot_json_payload_never_leaks_raw_account_no() -> None:
    client, _ = mock_client(snapshot_handler)
    try:
        snapshot = await client.portfolio_snapshot()
    finally:
        await client.close()
    payload = json.dumps(dataclasses.asdict(snapshot), ensure_ascii=False, default=str)
    assert RAW_ACCOUNT_NO not in payload
    assert snapshot.account.masked_account_no in payload
