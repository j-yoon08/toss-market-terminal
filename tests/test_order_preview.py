"""v0.7a paper-preview order domain tests (offline, no order endpoints)."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import subprocess
import sys
from decimal import Decimal

import pytest

from toss_market_terminal import order_preview as op
from toss_market_terminal.models import mask_account_no

RAW_ACCOUNT_NO = "50123456701"


def krw_base(**overrides: object) -> dict[str, object]:
    """BUY 005930 @50000 x1 → notional 50000 (cap/budget 내 통과 케이스)."""
    base: dict[str, object] = {
        "account_no": RAW_ACCOUNT_NO,
        "account_seq": 1,
        "symbol": "005930",
        "side": op.OrderSide.BUY,
        "order_type": op.OrderType.LIMIT,
        "quantity": "1",
        "limit_price": "50000",
        "reference_last_price": "50000",
        "holding_quantity": "0",
        "cash_buying_power": "500000",
    }
    base.update(overrides)
    return base


def usd_base(**overrides: object) -> dict[str, object]:
    """BUY AAPL @50 x2 → notional 100 USD (상한 경계와 동일한 통과 케이스)."""
    base: dict[str, object] = {
        "account_no": RAW_ACCOUNT_NO,
        "account_seq": 1,
        "symbol": "AAPL",
        "side": op.OrderSide.BUY,
        "order_type": op.OrderType.LIMIT,
        "quantity": "2",
        "limit_price": "50",
        "reference_last_price": "49",
        "holding_quantity": "10",
        "cash_buying_power": "150",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 엄격한 Decimal 입력 파서
# ---------------------------------------------------------------------------


def test_parser_accepts_string_and_int():
    assert op.parse_decimal_input("12.5", "quantity") == Decimal("12.5")
    assert op.parse_decimal_input(10, "quantity") == Decimal("10")


@pytest.mark.parametrize("value", [1.0, 0.5, float("nan"), float("inf")])
def test_parser_rejects_floats(value):
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input(value, "quantity")


@pytest.mark.parametrize("value", [True, False])
def test_parser_rejects_booleans(value):
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input(value, "quantity")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_parser_rejects_non_finite_strings(value):
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input(value, "quantity")


@pytest.mark.parametrize("value", ["0", "-1", "-0.0001", -1])
def test_parser_rejects_zero_and_negative(value):
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input(value, "quantity")


@pytest.mark.parametrize("value", ["1" * 31, "-" + "9" * 40])
def test_parser_rejects_overlong_input(value):
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input(value, "quantity")


@pytest.mark.parametrize("value", ["abc", "", None, ["1"], "1.2.3", " 1 ", "\uff11"])
def test_parser_rejects_non_numeric(value):
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input(value, "quantity")


def test_balance_fields_allow_zero_but_stay_validated():
    assert op.parse_decimal_input("0", "holding_quantity", allow_zero=True) == Decimal("0")
    with pytest.raises(op.OrderPreviewError):
        op.parse_decimal_input("-0", "cash_buying_power", allow_zero=True)


def test_canonical_decimal_text_is_stable_across_representations():
    assert op.canonical_decimal_text(Decimal("10")) == "10"
    assert op.canonical_decimal_text(Decimal("1E+1")) == "10"
    assert op.canonical_decimal_text(Decimal("0.500")) == "0.5"


# ---------------------------------------------------------------------------
# 불변 모델과 프라이버시 가드
# ---------------------------------------------------------------------------


def test_enum_values_are_exact():
    assert op.OrderSide.BUY.value == "BUY"
    assert op.OrderSide.SELL.value == "SELL"
    assert op.OrderType.LIMIT.value == "LIMIT"
    assert op.OrderType.MARKET.value == "MARKET"


def test_order_intent_is_frozen():
    preview = op.build_preview(**krw_base())
    with pytest.raises(dataclasses.FrozenInstanceError):
        preview.intent.quantity = Decimal("2")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        preview.intent.symbol = "AAPL"  # type: ignore[misc]


def test_order_preview_is_frozen():
    preview = op.build_preview(**krw_base())
    with pytest.raises(dataclasses.FrozenInstanceError):
        preview.mode = "LIVE"  # type: ignore[misc]


def test_intent_refuses_raw_account_number_storage():
    with pytest.raises(op.OrderPreviewError):
        op.OrderIntent(
            account_seq=1,
            masked_account_no=RAW_ACCOUNT_NO,
            symbol="005930",
            market="kr",
            currency="KRW",
            side=op.OrderSide.BUY,
            order_type=op.OrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=Decimal("50000"),
            reference_last_price=Decimal("50000"),
            holding_quantity=Decimal("0"),
            cash_buying_power=Decimal("500000"),
            time_in_force=op.TIME_IN_FORCE_DAY,
        )


def test_intent_keeps_only_masked_account():
    preview = op.build_preview(**krw_base())
    assert preview.intent.masked_account_no == mask_account_no(RAW_ACCOUNT_NO)
    assert RAW_ACCOUNT_NO not in repr(preview)


# ---------------------------------------------------------------------------
# 입력 검증 극성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_seq", [0, -3, True, "1", 1.0])
def test_account_seq_must_be_positive_integer(bad_seq):
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(account_seq=bad_seq))


@pytest.mark.parametrize("bad_symbol", ["!!!", "", "00593 0"])
def test_invalid_symbol_fails_closed(bad_symbol):
    with pytest.raises(ValueError):
        op.build_preview(**krw_base(symbol=bad_symbol))


def test_symbol_is_normalized():
    preview = op.build_preview(**usd_base(symbol=" aapl "))
    assert preview.intent.symbol == "AAPL"
    assert preview.intent.market == "us"
    assert preview.intent.currency == "USD"


@pytest.mark.parametrize("bad_price", [None, "0", "-1", "NaN"])
def test_limit_requires_positive_finite_price(bad_price):
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(limit_price=bad_price))


def test_market_order_must_not_carry_price():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(order_type=op.OrderType.MARKET))


def test_quantity_based_orders_only_no_order_amount():
    with pytest.raises(TypeError):
        op.build_preview(**krw_base(order_amount="50000"))  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 수량 정수 규칙 (시장 x 사이드 x 주문 유형)
# ---------------------------------------------------------------------------


def test_kr_fractional_quantity_rejected_for_limit():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(quantity="0.5"))


def test_kr_fractional_quantity_rejected_for_market_sell():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(
            **krw_base(
                side=op.OrderSide.SELL,
                order_type=op.OrderType.MARKET,
                quantity="1.5",
                holding_quantity="10",
                limit_price=None,
            )
        )


def test_us_limit_fractional_quantity_rejected():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**usd_base(quantity="2.5"))


def test_us_market_buy_fractional_quantity_rejected():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**usd_base(order_type=op.OrderType.MARKET, quantity="2.5"))


def test_us_market_sell_fractional_quantity_allowed():
    preview = op.build_preview(
        **usd_base(
            side=op.OrderSide.SELL,
            order_type=op.OrderType.MARKET,
            quantity="1.5",
            limit_price=None,
        )
    )
    assert preview.estimated_notional == Decimal("73.5")  # 1.5 x 참고가 49


def test_kr_market_sell_integer_quantity_allowed():
    preview = op.build_preview(
        **krw_base(
            side=op.OrderSide.SELL,
            order_type=op.OrderType.MARKET,
            quantity="2",
            holding_quantity="3",
            reference_last_price="40000",
            cash_buying_power="0",
            limit_price=None,
        )
    )
    assert preview.estimated_notional == Decimal("80000")


# ---------------------------------------------------------------------------
# 리스크 게이트: 예산 · 보유수량 · 하드캡 · 지원 조합
# ---------------------------------------------------------------------------


def test_buy_over_budget_blocked():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(cash_buying_power="49999.75"))


def test_buy_exactly_at_budget_allowed():
    preview = op.build_preview(**krw_base(cash_buying_power="50000"))
    assert preview.estimated_notional == Decimal("50000")


def test_oversell_blocked():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(side=op.OrderSide.SELL, quantity="3", holding_quantity="2"))


def test_sell_exactly_at_holding_allowed():
    preview = op.build_preview(
        **krw_base(
            side=op.OrderSide.SELL,
            quantity="2",
            holding_quantity="2",
            limit_price="40000",
        )
    )
    assert preview.estimated_notional == Decimal("80000")


def test_krw_hard_cap_blocks_above_default():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(quantity="2", limit_price="50001"))


def test_krw_hard_cap_boundary_allowed():
    preview = op.build_preview(**krw_base(quantity="2", limit_price="50000"))
    assert preview.estimated_notional == Decimal("100000")


def test_usd_hard_cap_blocks_above_default():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**usd_base(limit_price="50.01"))


def test_hard_caps_are_configurable():
    tight = op.RiskLimits(
        max_single_order_krw=Decimal("30000"),
        max_single_order_usd=Decimal("10"),
    )
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(limits=tight))
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**usd_base(limits=tight))
    loose = op.RiskLimits(
        max_single_order_krw=Decimal("1000000"),
        max_single_order_usd=Decimal("1000"),
    )
    assert op.build_preview(**krw_base(limits=loose)) is not None


def test_defaults_match_conservative_policy():
    limits = op.RiskLimits()
    assert limits.max_single_order_krw == Decimal("100000")
    assert limits.max_single_order_usd == Decimal("100")


def test_market_currency_override_mismatching_symbol_fails_closed():
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(currency="USD"))
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(market="us"))
    with pytest.raises(op.OrderPreviewError):
        op.build_preview(**krw_base(market="jp", currency="JPY"))


def test_risk_gate_rejects_unsupported_combination_directly():
    intent = op.OrderIntent(
        account_seq=1,
        masked_account_no="*******0001",
        symbol="7203",
        market="jp",
        currency="JPY",
        side=op.OrderSide.BUY,
        order_type=op.OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        reference_last_price=Decimal("100"),
        holding_quantity=Decimal("0"),
        cash_buying_power=Decimal("100000"),
        time_in_force=op.TIME_IN_FORCE_DAY,
    )
    with pytest.raises(op.OrderPreviewError):
        op.RiskGate.validate(intent, op.RiskLimits())


def test_estimate_uses_limit_price_for_limit_orders():
    preview = op.build_preview(**krw_base(limit_price="40000", reference_last_price="99999"))
    assert preview.estimated_notional == Decimal("40000")


def test_estimate_uses_reference_last_price_for_market_orders():
    preview = op.build_preview(
        **krw_base(
            order_type=op.OrderType.MARKET,
            limit_price=None,
            reference_last_price="60000",
        )
    )
    assert preview.estimated_notional == Decimal("60000")


def test_time_in_force_is_day():
    preview = op.build_preview(**krw_base())
    assert preview.intent.time_in_force == "DAY"


# ---------------------------------------------------------------------------
# 지문(fingerprint)과 승인 문구
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_across_builds():
    first = op.build_preview(**krw_base())
    second = op.build_preview(**krw_base())
    assert first.fingerprint == second.fingerprint


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("quantity", "2"),
        ("account_no", "99999999999"),
        ("limit_price", "49999"),
        ("reference_last_price", "49999"),
    ],
)
def test_fingerprint_changes_when_intent_changes(key, value):
    baseline = op.build_preview(**krw_base())
    altered = op.build_preview(**krw_base(**{key: value}))
    assert baseline.fingerprint != altered.fingerprint


def test_fingerprint_changes_with_side():
    baseline = op.build_preview(**krw_base(side=op.OrderSide.BUY, holding_quantity="5"))
    flipped = op.build_preview(**krw_base(side=op.OrderSide.SELL, holding_quantity="5"))
    assert baseline.fingerprint != flipped.fingerprint


def test_fingerprint_is_sha256_of_canonical_payload_without_timestamps():
    preview = op.build_preview(**krw_base())
    payload = {
        "safety_policy_version": op.SAFETY_POLICY_VERSION,
        "account_seq": 1,
        "masked_account_no": mask_account_no(RAW_ACCOUNT_NO),
        "symbol": "005930",
        "market": "kr",
        "currency": "KRW",
        "side": "BUY",
        "order_type": "LIMIT",
        "time_in_force": "DAY",
        "quantity": "1",
        "limit_price": "50000",
        "reference_last_price": "50000",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert preview.fingerprint == expected
    assert "timestamp" not in canonical


def test_approval_phrase_is_exact():
    preview = op.build_preview(**krw_base())
    expected_prefix = f"APPROVE BUY 005930 1 {preview.fingerprint[:8]}"
    assert preview.approval_phrase == expected_prefix
    assert preview.approval_phrase == (f"APPROVE BUY 005930 1 {preview.fingerprint[:8]}")
    other = op.build_preview(
        **usd_base(
            side=op.OrderSide.SELL,
            order_type=op.OrderType.MARKET,
            quantity="1.5",
            limit_price=None,
        )
    )
    assert other.approval_phrase == (f"APPROVE SELL AAPL 1.5 {other.fingerprint[:8]}")


# ---------------------------------------------------------------------------
# PaperPreviewService · 미리보기 플래그 · 직렬화
# ---------------------------------------------------------------------------


def test_paper_preview_flags_are_exact():
    preview = op.build_preview(**krw_base())
    assert preview.mode == "PAPER_PREVIEW"
    assert preview.order_endpoint_called is False
    assert preview.automatic_retry is False
    assert preview.manual_approval_only is True


def test_service_creates_preview_and_has_no_client_dependency():
    service = op.PaperPreviewService()
    # 서비스는 리스크 정책(limits) 외에는 어떤 상태·의존성도 갖지 않는다.
    assert [f.name for f in dataclasses.fields(service)] == ["limits"]
    preview = service.create_preview(**krw_base())
    assert preview.mode == "PAPER_PREVIEW"
    assert preview.order_endpoint_called is False
    assert not any("client" in name.lower() for name in dir(service))


def test_serialization_is_json_safe_and_masked():
    preview = op.build_preview(**krw_base())
    payload = preview.to_privacy_safe_dict()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert RAW_ACCOUNT_NO not in rendered
    assert "token" not in rendered.lower()
    assert payload["masked_account_no"] == mask_account_no(RAW_ACCOUNT_NO)
    assert payload["estimated_notional"] == "50000"
    assert payload["order_endpoint_called"] is False
    assert payload["manual_approval_only"] is True


@pytest.mark.parametrize(
    "break_kwargs",
    [
        {"cash_buying_power": "1"},
        {"side": op.OrderSide.SELL, "holding_quantity": "0"},
        {"quantity": "999999"},
    ],
)
def test_error_messages_never_leak_raw_account(break_kwargs):
    collected: list[str] = []
    for kwargs in ({}, break_kwargs):
        merged = {**krw_base(), **kwargs}
        try:
            op.build_preview(**merged)
        except op.OrderPreviewError as exc:
            collected.append(str(exc))
    joined = "\n".join(collected)
    assert joined, "최소 한 개의 오류가 발생해야 합니다"
    assert RAW_ACCOUNT_NO not in joined
    assert mask_account_no(RAW_ACCOUNT_NO) not in joined


# ---------------------------------------------------------------------------
# 모듈 위생: HTTP/주문 엔드포인트 부재
# ---------------------------------------------------------------------------

FORBIDDEN_TOKENS = (
    "httpx",
    "urllib",
    "socket",
    "requests.",
    "TossMarketClient",
    "api/v1/orders",
    "access_token",
    "client_secret",
)


def test_module_source_has_no_http_or_order_endpoint_references():
    source = inspect.getsource(op)
    lowered = source.lower()
    for token in FORBIDDEN_TOKENS:
        assert token.lower() not in lowered, f"금지 토큰 발견: {token}"


def test_importing_module_does_not_pull_http_clients():
    code = (
        "import sys, toss_market_terminal.order_preview as m;"
        "assert not {'httpx', 'urllib.request', 'socket'} & set(sys.modules),"
        "'network module imported'; print('CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout
