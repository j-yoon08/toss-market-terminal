"""v0.6 read-only account models: strict parsing, masking, privacy."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from toss_market_terminal.models import (
    Account,
    BuyingPower,
    HoldingsItem,
    HoldingsOverview,
    mask_account_no,
)


def official_account() -> dict[str, object]:
    # Exact shape from the official /api/v1/accounts 200 example.
    return {"accountNo": "12345678901", "accountSeq": 1, "accountType": "BROKERAGE"}


def official_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "marketCountry": "US",
        "currency": "USD",
        "quantity": "10",
        "lastPrice": "178.5",
        "averagePurchasePrice": "155.3",
        "marketValue": {
            "purchaseAmount": "1553",
            "amount": "1785",
            "amountAfterCost": "1771.43",
        },
        "profitLoss": {
            "amount": "232",
            "amountAfterCost": "218.43",
            "rate": "0.1494",
            "rateAfterCost": "0.1406",
        },
        "dailyProfitLoss": {"amount": "25", "rate": "0.0142"},
        "cost": {"commission": "3.57", "tax": "10"},
    }
    item.update(overrides)
    return item


def official_overview(items: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "totalPurchaseAmount": {"krw": "6500000", "usd": "1553"},
        "marketValue": {
            "amount": {"krw": "7200000", "usd": "1785"},
            "amountAfterCost": {"krw": "7050000", "usd": "1771.43"},
        },
        "profitLoss": {
            "amount": {"krw": "700000", "usd": "232"},
            "amountAfterCost": {"krw": "550000", "usd": "218.43"},
            "rate": "0.1179",
            "rateAfterCost": "0.0983",
        },
        "dailyProfitLoss": {"amount": {"krw": "100000", "usd": "25"}, "rate": "0.0141"},
        "items": [official_item()] if items is None else items,
    }


# --- Account ----------------------------------------------------------------


def test_account_parses_official_example_and_masks_number() -> None:
    account = Account.from_api(official_account())
    assert account.account_seq == 1
    assert account.account_type == "BROKERAGE"
    assert account.masked_account_no == "*******8901"


def test_account_never_stores_raw_account_number() -> None:
    account = Account.from_api(official_account())
    dumped = repr(account) + str(dataclasses.asdict(account))
    assert "12345678901" not in dumped
    assert not any(field.name == "account_no" for field in dataclasses.fields(account))


def test_account_preserves_unknown_enum_string() -> None:
    raw = official_account()
    raw["accountType"] = "MYSTERY_TYPE"
    assert Account.from_api(raw).account_type == "MYSTERY_TYPE"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"accountNo": "12345678901", "accountSeq": 1},
        {"accountSeq": 1, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountType": "BROKERAGE"},
        {"accountNo": "", "accountSeq": 1, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": "1", "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": 1.0, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": True, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": 0, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": -3, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": 1, "accountType": ""},
        {"accountNo": 12345678901, "accountSeq": 1, "accountType": "BROKERAGE"},
        ["accountNo"],
    ],
)
def test_account_malformed_shapes_fail_closed(raw: object) -> None:
    with pytest.raises(Exception) as caught:
        Account.from_api(raw)  # type: ignore[arg-type]
    assert isinstance(caught.value, ValueError)
    assert "12345678901" not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12345678901", "*******8901"), ("ABCD1234", "****1234"), ("12", "**"), ("1234", "****")],
)
def test_mask_account_no(value: str, expected: str) -> None:
    assert mask_account_no(value) == expected


# --- HoldingsOverview -------------------------------------------------------


def test_holdings_overview_parses_official_envelope() -> None:
    overview = HoldingsOverview.from_api(official_overview())
    assert overview.total_purchase_amount.krw == Decimal("6500000")
    assert overview.total_purchase_amount.usd == Decimal("1553")
    assert overview.market_value.amount.usd == Decimal("1785")
    assert overview.profit_loss.rate_after_cost == Decimal("0.0983")
    assert overview.daily_profit_loss.amount.krw == Decimal("100000")
    assert overview.daily_profit_loss.rate == Decimal("0.0141")
    assert len(overview.items) == 1
    item = overview.items[0]
    assert item.symbol == "AAPL"
    assert item.quantity == Decimal("10")
    assert item.last_price == Decimal("178.5")
    assert item.average_purchase_price == Decimal("155.3")
    assert item.market_value.purchase_amount == Decimal("1553")
    assert item.market_value.amount_after_cost == Decimal("1771.43")
    assert item.profit_loss.rate == Decimal("0.1494")
    assert item.daily_profit_loss.amount == Decimal("25")
    assert item.cost.commission == Decimal("3.57")
    assert item.cost.tax == Decimal("10")


def test_holdings_item_preserves_unknown_enum_strings() -> None:
    item = HoldingsItem.from_api(official_item(marketCountry="MOON", currency="XRD"))
    assert item.market_country == "MOON"
    assert item.currency == "XRD"


def test_holdings_null_usd_and_tax_parse_to_none() -> None:
    raw = official_overview(
        [
            official_item(
                marketCountry="KR",
                currency="KRW",
                cost={"commission": "14400", "tax": None},
            )
        ]
    )
    raw["totalPurchaseAmount"] = {"krw": "6500000", "usd": None}
    raw["marketValue"]["amount"]["usd"] = None
    overview = HoldingsOverview.from_api(raw)
    assert overview.total_purchase_amount.usd is None
    assert overview.items[0].cost.tax is None
    assert overview.items[0].currency == "KRW"


def test_holdings_empty_items_allowed() -> None:
    raw = official_overview(items=[])
    raw["totalPurchaseAmount"] = {"krw": "0", "usd": None}
    overview = HoldingsOverview.from_api(raw)
    assert overview.items == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.pop("totalPurchaseAmount"),
        lambda raw: raw.pop("marketValue"),
        lambda raw: raw.pop("profitLoss"),
        lambda raw: raw.pop("dailyProfitLoss"),
        lambda raw: raw.pop("items"),
        lambda raw: raw["totalPurchaseAmount"].pop("krw"),
        lambda raw: raw["profitLoss"].pop("rate"),
        lambda raw: raw.update(items={"symbol": "AAPL"}),
        lambda raw: raw.update(items=[official_item(quantity="NaN")]),
        lambda raw: raw.update(items=[official_item(quantity="Infinity")]),
        lambda raw: raw.update(items=[official_item(quantity="1" + "0" * 30)]),
        lambda raw: raw.update(items=[official_item(quantity=100.5)]),
        lambda raw: raw.update(items=[official_item(lastPrice=None)]),
        lambda raw: raw.update(items=[official_item(cost={"commission": "1"})]),
        lambda raw: raw.update(items=[official_item(cost={"commission": "1", "tax": "x,y"})]),
        lambda raw: raw["items"][0].pop("symbol"),
    ],
)
def test_holdings_malformed_shapes_fail_closed(mutate: object) -> None:
    raw = official_overview()
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(ValueError):
        HoldingsOverview.from_api(raw)


def test_holdings_rejects_non_finite_and_overlong_decimals() -> None:
    for bad in ("NaN", "nan", "Infinity", "-Infinity", "1" + "0" * 30):
        with pytest.raises(ValueError):
            HoldingsOverview.from_api(official_overview([official_item(quantity=bad)]))


# --- BuyingPower ------------------------------------------------------------


def test_buying_power_parses_official_examples() -> None:
    krw = BuyingPower.from_api({"currency": "KRW", "cashBuyingPower": "5000000"})
    usd = BuyingPower.from_api({"currency": "USD", "cashBuyingPower": "3500.5"})
    assert krw.cash_buying_power == Decimal("5000000")
    assert usd.cash_buying_power == Decimal("3500.5")
    assert usd.currency == "USD"


def test_buying_power_preserves_unknown_currency_string() -> None:
    power = BuyingPower.from_api({"currency": "EUR", "cashBuyingPower": "1"})
    assert power.currency == "EUR"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"currency": "KRW"},
        {"cashBuyingPower": "1"},
        {"currency": "", "cashBuyingPower": "1"},
        {"currency": "KRW", "cashBuyingPower": "NaN"},
        {"currency": "KRW", "cashBuyingPower": 1.5},
        {"currency": "KRW", "cashBuyingPower": None},
        "buying-power",
    ],
)
def test_buying_power_malformed_shapes_fail_closed(raw: object) -> None:
    with pytest.raises(ValueError):
        BuyingPower.from_api(raw)  # type: ignore[arg-type]
