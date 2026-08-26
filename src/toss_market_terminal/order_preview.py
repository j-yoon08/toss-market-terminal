"""v0.7a paper/preview order domain layer.

이 모듈은 주문 **미리보기(paper preview)** 전용 도메인 계층입니다.

경계 선언:
  * HTTP 클라이언트 라이브러리에 의존하지 않고 네트워크 호출을 하지 않는다.
  * 주문 엔드포인트를 알지 못하며 호출할 수 없다. 미리보기 생성만 한다.
  * 원문 계좌번호·토큰은 저장 불가능하며, 마스킹된 계좌번호만 보관한다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum

from toss_market_terminal.models import (
    infer_market,
    mask_account_no,
    normalize_symbol,
)

SAFETY_POLICY_VERSION = "0.7a-paper"
TIME_IN_FORCE_DAY = "DAY"

SUPPORTED_MARKETS = frozenset({"kr", "us"})
SUPPORTED_CURRENCIES = frozenset({"KRW", "USD"})
MARKET_CURRENCY = {"kr": "KRW", "us": "USD"}

# 입력 텍스트 길이 상한(모델 계층의 decimal 상한과 동일한 보수적 값).
MAX_DECIMAL_TEXT_LENGTH = 30
MASKED_ACCOUNT_MAX_LENGTH = 64

# 순수 ASCII decimal 텍스트만 허용(전각 숫사·공백·NaN/Infinity 차단).
DECIMAL_INPUT_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _positive_int(value: object, field_name: str) -> int:
    """bool/float/문자열을 거부하고 양의 정수만 허용한다(OrderPreviewError)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderPreviewError(f"{field_name} 값은 정수여야 합니다.")
    if value <= 0:
        raise OrderPreviewError(f"{field_name} 값은 양의 정수여야 합니다.")
    return value


class OrderPreviewError(ValueError):
    """주문 미리보기 생성 또는 리스크 게이트 통과에 실패했습니다(fail-closed)."""


def parse_decimal_input(
    value: object,
    field_name: str,
    *,
    allow_zero: bool = False,
) -> Decimal:
    """문자열/정수만 받아 유한하고 양수인 Decimal로 변환한다.

    bool, float, NaN/Infinity 문자열, 0(allow_zero=False일 때), 음수,
    과도하게 긴 텍스트는 모두 거부한다.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OrderPreviewError(f"{field_name} 값은 decimal 문자열 또는 정수여야 합니다.")
    text = str(value)
    if len(text) > MAX_DECIMAL_TEXT_LENGTH:
        raise OrderPreviewError(f"{field_name} 값의 자릿수가 너무 깁니다.")
    if isinstance(value, str) and not DECIMAL_INPUT_PATTERN.fullmatch(text):
        raise OrderPreviewError(f"{field_name} 값이 올바른 decimal 텍스트가 아닙니다.")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise OrderPreviewError(f"{field_name} 값을 Decimal로 변환할 수 없습니다: {value}") from exc
    if not parsed.is_finite():
        raise OrderPreviewError(f"{field_name} 값은 유한한 수여야 합니다.")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        sign = "0 이상" if allow_zero else "양수"
        raise OrderPreviewError(f"{field_name} 값은 {sign}이어야 합니다.")
    return parsed


def canonical_decimal_text(value: Decimal) -> str:
    """지문 직렬화를 위해 지수·후행 0을 제거한 안정적인 텍스트로 변환한다."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        normalized = normalized.quantize(Decimal("1"))
    return format(normalized, "f")


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """단일 주문 추정 금액의 안전 상한. 투자 권고가 아닌 보수적 기본값이다."""

    max_single_order_krw: Decimal = Decimal("100000")
    max_single_order_usd: Decimal = Decimal("100")


def _masked_account(account_no: str) -> str:
    if not isinstance(account_no, str) or not account_no:
        raise OrderPreviewError("계좌번호는 비어 있지 않은 문자열이어야 합니다.")
    masked = mask_account_no(account_no)
    if len(masked) > MASKED_ACCOUNT_MAX_LENGTH:
        raise OrderPreviewError("계좌번호 형식이 올바르지 않습니다.")
    return masked


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """불변 주문 의도. 원문 계좌번호는 저장 구조상 불가능하다."""

    account_seq: int
    masked_account_no: str
    symbol: str
    market: str
    currency: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    reference_last_price: Decimal
    holding_quantity: Decimal
    cash_buying_power: Decimal
    time_in_force: str = TIME_IN_FORCE_DAY

    def __post_init__(self) -> None:
        if (
            isinstance(self.masked_account_no, str)
            and len(self.masked_account_no.replace("*", "")) > 4
        ):
            # 마스킹되지 않은 값(원문 계좌번호)은 저장을 거부한다.
            raise OrderPreviewError("원문 계좌번호는 저장할 수 없습니다. 마스킹된 값만 허용됩니다.")

    def fingerprint_payload(self) -> dict[str, str | int | None]:
        return {
            "safety_policy_version": SAFETY_POLICY_VERSION,
            "account_seq": self.account_seq,
            "masked_account_no": self.masked_account_no,
            "symbol": self.symbol,
            "market": self.market,
            "currency": self.currency,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "time_in_force": self.time_in_force,
            "quantity": canonical_decimal_text(self.quantity),
            "limit_price": (
                None if self.limit_price is None else canonical_decimal_text(self.limit_price)
            ),
            "reference_last_price": canonical_decimal_text(self.reference_last_price),
        }


@dataclass(frozen=True, slots=True)
class OrderPreview:
    """PAPER_PREVIEW 결과물. 어떤 경로로도 주문을 전송하지 않는다."""

    intent: OrderIntent
    mode: str
    order_endpoint_called: bool
    automatic_retry: bool
    manual_approval_only: bool
    estimated_notional: Decimal
    fingerprint: str
    approval_phrase: str

    def to_privacy_safe_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "order_endpoint_called": self.order_endpoint_called,
            "automatic_retry": self.automatic_retry,
            "manual_approval_only": self.manual_approval_only,
            "account_seq": self.intent.account_seq,
            "masked_account_no": self.intent.masked_account_no,
            "symbol": self.intent.symbol,
            "market": self.intent.market,
            "currency": self.intent.currency,
            "side": self.intent.side.value,
            "order_type": self.intent.order_type.value,
            "quantity": canonical_decimal_text(self.intent.quantity),
            "limit_price": (
                None
                if self.intent.limit_price is None
                else canonical_decimal_text(self.intent.limit_price)
            ),
            "reference_last_price": canonical_decimal_text(self.intent.reference_last_price),
            "holding_quantity": canonical_decimal_text(self.intent.holding_quantity),
            "cash_buying_power": canonical_decimal_text(self.intent.cash_buying_power),
            "time_in_force": self.intent.time_in_force,
            "estimated_notional": canonical_decimal_text(self.estimated_notional),
            "fingerprint": self.fingerprint,
            "approval_phrase": self.approval_phrase,
            "safety_policy_version": SAFETY_POLICY_VERSION,
        }


def _validate_market_currency(market: str, currency: str) -> None:
    if market not in SUPPORTED_MARKETS or currency not in SUPPORTED_CURRENCIES:
        raise OrderPreviewError(
            f"지원하지 않는 시장/통화 조합입니다: market={market}, currency={currency}"
        )
    if MARKET_CURRENCY[market] != currency:
        raise OrderPreviewError(
            f"시장과 통화가 일치하지 않습니다: market={market}, currency={currency}"
        )


def _require_integer_quantity(intent: OrderIntent) -> None:
    if intent.quantity != intent.quantity.to_integral_value():
        raise OrderPreviewError(
            f"{intent.market.upper()} 시장의 이 주문에는 정수 수량만 허용됩니다."
        )


class RiskGate:
    """미리보기 주문 의도에 대한 fail-closed 검증기."""

    @staticmethod
    def validate(intent: OrderIntent, limits: RiskLimits) -> None:
        _validate_market_currency(intent.market, intent.currency)
        if intent.account_seq <= 0 or isinstance(intent.account_seq, bool):
            raise OrderPreviewError("accountSeq 값은 양의 정수여야 합니다.")

        estimated_notional = RiskGate.estimate(intent)

        if intent.side is OrderSide.SELL:
            if intent.quantity > intent.holding_quantity:
                raise OrderPreviewError(
                    "매도 수량이 보유 수량을 초과합니다: "
                    f"{canonical_decimal_text(intent.quantity)} > "
                    f"{canonical_decimal_text(intent.holding_quantity)}"
                )
        elif estimated_notional > intent.cash_buying_power:
            raise OrderPreviewError(
                "추정 주문 금액이 매수가능금액을 초과합니다: "
                f"{canonical_decimal_text(estimated_notional)} > "
                f"{canonical_decimal_text(intent.cash_buying_power)}"
            )

        if intent.market == "kr":
            _require_integer_quantity(intent)
            cap = limits.max_single_order_krw
        else:
            if intent.order_type is OrderType.MARKET and intent.side is OrderSide.BUY:
                _require_integer_quantity(intent)
            elif intent.order_type is OrderType.LIMIT:
                _require_integer_quantity(intent)
            cap = limits.max_single_order_usd

        if estimated_notional > cap:
            raise OrderPreviewError(
                f"단일 주문 추정 금액이 안전 상한({canonical_decimal_text(cap)} "
                f"{intent.currency})을 초과합니다: "
                f"{canonical_decimal_text(estimated_notional)} {intent.currency}. "
                "이 상한은 안전 장치이며 투자 권고가 아닙니다."
            )

    @staticmethod
    def estimate(intent: OrderIntent) -> Decimal:
        """LIMIT은 지정가, MARKET은 참고 현재가로 추정 금액을 계산한다."""
        unit_price = (
            intent.limit_price
            if intent.order_type is OrderType.LIMIT
            else intent.reference_last_price
        )
        return intent.quantity * unit_price


@dataclass(frozen=True, slots=True)
class PaperPreviewService:
    """미리보기만 생성하는 서비스. HTTP 클라이언트 의존성이 없다."""

    limits: RiskLimits = field(default_factory=RiskLimits)

    @staticmethod
    def approval_phrase_for(intent: OrderIntent, fingerprint: str) -> str:
        quantity = canonical_decimal_text(intent.quantity)
        return f"APPROVE {intent.side.value} {intent.symbol} {quantity} {fingerprint[:8]}"

    def create_preview(
        self,
        account_no: str,
        account_seq: int,
        symbol: str,
        side: OrderSide | str,
        order_type: OrderType | str,
        quantity: object,
        reference_last_price: object,
        holding_quantity: object,
        cash_buying_power: object,
        limit_price: object = None,
        market: str | None = None,
        currency: str | None = None,
        time_in_force: str = TIME_IN_FORCE_DAY,
        limits: RiskLimits | None = None,
    ) -> OrderPreview:
        return build_preview(
            account_no=account_no,
            account_seq=account_seq,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            reference_last_price=reference_last_price,
            holding_quantity=holding_quantity,
            cash_buying_power=cash_buying_power,
            limit_price=limit_price,
            market=market,
            currency=currency,
            time_in_force=time_in_force,
            limits=self.limits if limits is None else limits,
        )


def build_preview(
    account_no: str,
    account_seq: int,
    symbol: str,
    side: OrderSide | str,
    order_type: OrderType | str,
    quantity: object,
    reference_last_price: object,
    holding_quantity: object,
    cash_buying_power: object,
    limit_price: object = None,
    market: str | None = None,
    currency: str | None = None,
    time_in_force: str = TIME_IN_FORCE_DAY,
    limits: RiskLimits | None = None,
) -> OrderPreview:
    """엄격하게 검증된 PAPER_PREVIEW OrderPreview를 만든다."""
    if isinstance(side, str):
        try:
            side = OrderSide(side.strip().upper())
        except ValueError as exc:
            raise OrderPreviewError(f"지원하지 않는 매매 방향입니다: {side}") from exc
    if isinstance(order_type, str):
        try:
            order_type = OrderType(order_type.strip().upper())
        except ValueError as exc:
            raise OrderPreviewError(f"지원하지 않는 주문 유형입니다: {order_type}") from exc

    seq = _positive_int(account_seq, "accountSeq")

    normalized_symbol = normalize_symbol(str(symbol))
    inferred_market = infer_market(normalized_symbol)
    resolved_market = inferred_market if market is None else str(market).strip().lower()
    resolved_currency = (
        MARKET_CURRENCY[inferred_market] if currency is None else str(currency).strip().upper()
    )
    _validate_market_currency(resolved_market, resolved_currency)

    qty = parse_decimal_input(quantity, "quantity")
    ref_price = parse_decimal_input(reference_last_price, "reference_last_price")
    holdings = parse_decimal_input(holding_quantity, "holding_quantity", allow_zero=True)
    buying_power = parse_decimal_input(cash_buying_power, "cash_buying_power", allow_zero=True)

    if order_type is OrderType.LIMIT:
        if limit_price is None:
            raise OrderPreviewError("LIMIT 주문에는 양수 지정가가 필요합니다.")
        price = parse_decimal_input(limit_price, "limit_price")
    else:
        if limit_price is not None:
            raise OrderPreviewError("MARKET 주문에는 지정가를 넣을 수 없습니다.")
        price = None

    if time_in_force != TIME_IN_FORCE_DAY:
        raise OrderPreviewError("v0.7a에서는 time_in_force=DAY만 지원합니다.")

    intent = OrderIntent(
        account_seq=seq,
        masked_account_no=_masked_account(account_no),
        symbol=normalized_symbol,
        market=resolved_market,
        currency=resolved_currency,
        side=side,
        order_type=order_type,
        quantity=qty,
        limit_price=price,
        reference_last_price=ref_price,
        holding_quantity=holdings,
        cash_buying_power=buying_power,
        time_in_force=time_in_force,
    )

    RiskGate.validate(intent, RiskLimits() if limits is None else limits)

    payload = json.dumps(
        intent.fingerprint_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    return OrderPreview(
        intent=intent,
        mode="PAPER_PREVIEW",
        order_endpoint_called=False,
        automatic_retry=False,
        manual_approval_only=True,
        estimated_notional=qty * (price if price is not None else ref_price),
        fingerprint=fingerprint,
        approval_phrase=PaperPreviewService.approval_phrase_for(intent, fingerprint),
    )
