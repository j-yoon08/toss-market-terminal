"""v0.8a manual-live execution PLAN/GATE core.

이 모듈은 수동 라이브 주문의 "계획 → 패킷 → 게이트 → 1회 제출" 코어만 담당한다.
아직 어떤 전송 구현도 연결되어 있지 않다(연결은 본 단계 이후 별도로 진행한다).

경계 선언:
  * HTTP 클라이언트 라이브러리에 의존하지 않고 네트워크 호출을 하지 않는다.
    실제 제출은 호출자가 주입한 LiveOrderTransport가 수행한다.
  * 주문 엔드포인트·URL·경로를 알지 못하며 만들지 않는다. 자격증명에 접근하지 않는다.
  * 실행 게이트는 호출 시점에 평가하며 하나라도 빠지면 fail-closed로 차단한다.
    게이트 상태는 디스크에 저장하지 않고, 원장은 프로세스 메모리에만 존재한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from toss_market_terminal.order_preview import (
    SAFETY_POLICY_VERSION,
    OrderIntent,
    OrderPreview,
    canonical_decimal_text,
)

LIVE_SAFETY_POLICY_VERSION = "0.8a-manual-live"
RISK_LIMITS_VERSION = "risk-limits/0.7a"
MANUAL_LIVE_ENV_KEY = "TOSS_ENABLE_MANUAL_LIVE_ORDERS"
MANUAL_LIVE_ENV_VALUE = "1"
DEFAULT_TTL_SECONDS = 300


_HEX64 = re.compile(r"[0-9a-f]{64}")

_ALLOWED_TIF = frozenset({"DAY"})
SUBMITTED_NOTE = "submitted; acceptance only"


class LiveOrderError(ValueError):
    """라이브 계획/패킷 생성 또는 검증 실패(fail-closed)."""


class LiveOrderTransportError(Exception):
    """transport.submit이 거절로 끝났을 때 사용하는 정제된 오류.

    반드시 안전한 짧은 코드만 담아야 하며, 원문 응답·오류 본문을 담아서는 안 된다.
    """

    def __init__(self, safe_code: str) -> None:
        super().__init__(safe_code)
        self.safe_code = safe_code


@dataclass(frozen=True, slots=True)
class LiveOrderPlan:
    """만료 가능한 라이브 주문 계획. 프리뷰 지문에 묶인 불변 스냅샷."""

    preview_fingerprint: str
    safety_policy: str
    intent: OrderIntent
    risk_limits_version: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LiveExecutionRequest:
    """호출 시점 게이트 입력. 세 개의 명시적 동의 플래그를 요구한다."""

    plan: LiveOrderPlan
    execute: bool
    acknowledge_final_approval: bool
    interactive_session: bool


class OrderSideLive(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderTypeLive(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True, slots=True)
class LiveOrderPacket:
    """수량 기반 공식 페이로드. fingerprint는 프리뷰 지문에 묶여 있다."""

    client_order_id: str
    fingerprint: str
    symbol: str
    side: OrderSideLive
    quantity: Decimal
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LiveOrderAccepted:
    """제출이 브로커에 접수된 상태. 체결(filled)과는 무관하다."""

    status: str  # 항상 "accepted"
    order_id: str
    client_order_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        if self.status != "accepted":
            raise LiveOrderError("LiveOrderAccepted의 status는 'accepted'여야 합니다.")

    def to_privacy_safe_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "fingerprint": self.fingerprint,
            "note": SUBMITTED_NOTE,
        }


@dataclass(frozen=True, slots=True)
class LiveOrderRejected:
    """게이트 차단 또는 transport 거절. 원문 오류는 절대 보관하지 않는다."""

    status: str  # "blocked" | "rejected"
    reason_codes: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        if self.status not in {"blocked", "rejected"}:
            raise LiveOrderError("LiveOrderRejected의 status는 blocked/rejected여야 합니다.")


@dataclass(frozen=True, slots=True)
class LiveOrderAmbiguous:
    """제출 결과를 알 수 없는 상태(타임아웃·끊긴 연결·판독 불가 응답).

    절대 재시도하지 않고, 원문 정보 없이 안전한 코드만 남긴다.
    """

    status: str  # 항상 "ambiguous"
    safe_code: str
    detail: str

    def __post_init__(self) -> None:
        if self.status != "ambiguous":
            raise LiveOrderError("LiveOrderAmbiguous의 status는 'ambiguous'여야 합니다.")


@dataclass(frozen=True, slots=True)
class LiveAuditRecord:
    """정제된 감사 레코드. 계좌번호·토큰·원문 응답·승인 문구는 필드 자체가 없다."""

    fingerprint: str
    client_order_id: str
    side: str
    symbol: str
    quantity: Decimal
    attempted_at: datetime
    status: str
    safe_code: str


@runtime_checkable
class LiveOrderTransport(Protocol):
    """호출자가 주입하는 전송 경로. 본 모듈에는 구현이 없다(연결은 이후 단계)."""

    def submit(self, packet: LiveOrderPacket) -> Mapping[str, object]: ...


LiveOutcome = LiveOrderAccepted | LiveOrderRejected | LiveOrderAmbiguous


def live_approval_phrase(packet: LiveOrderPacket) -> str:
    """패킷에 묶인 라이브 승인 문구. 반드시 64자 전체 지문을 포함한다."""
    return (
        f"CONFIRM LIVE {packet.side.value} {packet.symbol} "
        f"{canonical_decimal_text(packet.quantity)} {packet.fingerprint}"
    )


def _recompute_preview_fingerprint(intent: OrderIntent) -> str:
    payload = json.dumps(
        intent.fingerprint_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LiveOrderError(f"{name} 값은 타임존 정보가 있는 datetime이어야 합니다.")
    return value.astimezone(UTC)


def create_live_plan(
    preview: OrderPreview,
    now: datetime,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> LiveOrderPlan:
    """유효한 PAPER_PREVIEW만 라이브 계획으로 승격한다(fail-closed).

    프리뷰의 안전 플래그를 재검증하고, canonical intent 페이로드로부터
    SHA-256 지문을 재계산해 위조 여부를 확인한다.
    """
    if not isinstance(preview, OrderPreview):
        raise LiveOrderError("OrderPreview(PAPER_PREVIEW) 객체만 라이브 계획으로 만들 수 있습니다.")
    if preview.mode != "PAPER_PREVIEW":
        raise LiveOrderError("PAPER_PREVIEW 모드만 라이브 계획으로 승격할 수 있습니다.")
    if preview.order_endpoint_called is not False or not isinstance(
        preview.order_endpoint_called, bool
    ):
        raise LiveOrderError("order_endpoint_called=false인 미리보기만 허용됩니다.")
    if preview.automatic_retry is not False or not isinstance(preview.automatic_retry, bool):
        raise LiveOrderError("automatic_retry=false인 미리보기만 허용됩니다.")
    if preview.manual_approval_only is not True or not isinstance(
        preview.manual_approval_only, bool
    ):
        raise LiveOrderError("manual_approval_only=true인 미리보기만 허용됩니다.")

    recomputed = _recompute_preview_fingerprint(preview.intent)
    if recomputed != preview.fingerprint:
        raise LiveOrderError("미리보기 지문이 의도 페이로드와 일치하지 않습니다(위조 가능).")
    if not _HEX64.fullmatch(str(preview.fingerprint)):
        raise LiveOrderError("미리보기 지문은 64자 소문자 hex여야 합니다.")
    unit_price = (
        preview.intent.limit_price
        if preview.intent.order_type.value == "LIMIT"
        else preview.intent.reference_last_price
    )
    if unit_price is None:
        raise LiveOrderError("주문 가격 기준값이 누락되었습니다.")
    expected_notional = preview.intent.quantity * unit_price
    if preview.estimated_notional != expected_notional:
        raise LiveOrderError("미리보기 추정 금액이 의도와 일치하지 않습니다(위조 가능).")

    now_utc = _require_utc(now, "now")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise LiveOrderError("ttl_seconds 값은 양의 정수여야 합니다.")

    return LiveOrderPlan(
        preview_fingerprint=preview.fingerprint,
        safety_policy=SAFETY_POLICY_VERSION,
        intent=preview.intent,
        risk_limits_version=RISK_LIMITS_VERSION,
        created_at=now_utc,
        expires_at=now_utc + timedelta(seconds=ttl_seconds),
    )


def client_order_id_for(fingerprint: str) -> str:
    """지문에서 결정적으로 파생되는 멱등 클라이언트 주문 ID(`tmt-` + 32 hex, ≤36자)."""
    if not _HEX64.fullmatch(str(fingerprint)):
        raise LiveOrderError("클라이언트 주문 ID는 64자 지문에서 파생해야 합니다.")
    digest = hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()[:32]
    return f"tmt-{digest}"


def build_live_packet(plan: LiveOrderPlan) -> LiveOrderPacket:
    """계획으로부터 수량 기반 공식 페이로드를 만든다(금액 필드는 존재하지 않는다)."""
    intent = plan.intent
    if plan.safety_policy != SAFETY_POLICY_VERSION:
        raise LiveOrderError("계획의 안전 정책 버전이 현재와 일치하지 않습니다.")
    recomputed = _recompute_preview_fingerprint(intent)
    if recomputed != plan.preview_fingerprint:
        raise LiveOrderError("계획 지문이 의도와 일치하지 않습니다(위조 가능).")
    if intent.time_in_force not in _ALLOWED_TIF:
        raise LiveOrderError("지원하는 time_in_force는 DAY뿐입니다.")

    side = OrderSideLive(intent.side.value)
    order_type = OrderTypeLive(intent.order_type.value)
    quantity_text = canonical_decimal_text(intent.quantity)
    payload: dict[str, object] = {
        "clientOrderId": client_order_id_for(plan.preview_fingerprint),
        "symbol": intent.symbol,
        "side": side.value,
        "orderType": order_type.value,
        # Toss OpenAPI OrderCreateQuantityBased.quantity is a decimal string.
        # Keeping it as text also avoids float precision loss for fractional US sells.
        "quantity": quantity_text,
        "confirmHighValueOrder": False,
    }
    if order_type is OrderTypeLive.LIMIT:
        payload["timeInForce"] = intent.time_in_force
        payload["price"] = canonical_decimal_text(
            intent.limit_price if intent.limit_price is not None else Decimal("0")
        )
    else:
        payload["timeInForce"] = intent.time_in_force

    return LiveOrderPacket(
        client_order_id=str(payload["clientOrderId"]),
        fingerprint=plan.preview_fingerprint,
        symbol=intent.symbol,
        side=side,
        quantity=intent.quantity,
        payload=payload,
    )


def _strict_accepted_response(raw: object, expected_client_order_id: str) -> tuple[str, str] | None:
    """응답이 {order_id(비어있지 않음), client_order_id(일치)} 정확한 구조일 때만 통과."""
    if not isinstance(raw, Mapping):
        return None
    if set(raw.keys()) != {"order_id", "client_order_id"}:
        return None
    order_id = raw.get("order_id")
    client_order_id = raw.get("client_order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        return None
    if client_order_id != expected_client_order_id:
        return None
    return order_id, expected_client_order_id


def _safe_reason(exc: Exception) -> str:
    """예외에서 원문 정보를 전혀 노출하지 않는 안전한 짧은 코드만 뽑아낸다."""
    if isinstance(exc, LiveOrderTransportError):
        return exc.safe_code
    return "TRANSPORT_SUBMIT_ERROR"


_GATE_ORDER = (
    "EXECUTE_FLAG_FALSE",
    "FINAL_APPROVAL_NOT_ACKNOWLEDGED",
    "NOT_INTERACTIVE_SESSION",
    "APPROVAL_PHRASE_MISMATCH",
    "MANUAL_LIVE_ORDERS_DISABLED",
    "PLAN_EXPIRED",
    "PACKET_FINGERPRINT_MISMATCH",
    "TRANSPORT_NOT_AVAILABLE",
)


def _missing_gates(
    request: LiveExecutionRequest,
    approval_phrase: str,
    packet: LiveOrderPacket,
    now_utc: datetime,
    plan: LiveOrderPlan,
    transport: LiveOrderTransport | None,
) -> str:
    """요구 게이트를 순서대로 평가하고, 하나라도 충족하지 않으면 안전한 코드를 반환한다."""
    if request.execute is not True:
        return _GATE_ORDER[0]
    if request.acknowledge_final_approval is not True:
        return _GATE_ORDER[1]
    if request.interactive_session is not True:
        return _GATE_ORDER[2]
    # 라이브 승인 문구는 반드시 패킷에 묶인 64자 전체 지문과 정확히 일치해야 한다.
    if approval_phrase != live_approval_phrase(packet):
        return _GATE_ORDER[3]
    if os.environ.get(MANUAL_LIVE_ENV_KEY) != MANUAL_LIVE_ENV_VALUE:
        return _GATE_ORDER[4]
    if now_utc > plan.expires_at:  # 만료 시각까지는 유효(경계 포함), 그 이후 차단
        return _GATE_ORDER[5]
    # 패킷·계획·프리뷰 지문이 정확히 재계산되는지 다시 확인한다.
    if (
        packet.fingerprint != plan.preview_fingerprint
        or _recompute_preview_fingerprint(plan.intent) != plan.preview_fingerprint
    ):
        return _GATE_ORDER[6]
    if transport is None:
        return _GATE_ORDER[7]
    return ""


class ManualLiveOrderExecutor:
    """게이트를 모두 통과한 계획을 주입된 transport로 정확히 한 번 제출한다.

    * 재시도 루프·타임아웃 재시도가 없다. 모호한 실패 뒤에는 절대 다시 제출하지 않는다.
    * 같은 지문의 동시 실행은 잠금으로 직렬화되며, 두 번째 호출은 already attempted로 차단된다.
    * 시도 원장은 프로세스 메모리에만 존재한다(디스크 저장 없음).
    """

    def __init__(self, transport: LiveOrderTransport | None) -> None:
        self._transport = transport
        self._lock = threading.Lock()
        self._attempted: dict[str, bool] = {}
        self._audit: list[LiveAuditRecord] = []

    # -- 공개 관찰자 -------------------------------------------------------

    def attempts(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._attempted)

    def audit_records(self) -> tuple[LiveAuditRecord, ...]:
        with self._lock:
            return tuple(self._audit)

    # -- 내부 유틸리티 -----------------------------------------------------

    def _blocked(self, reason_code: str) -> LiveOrderRejected:
        return LiveOrderRejected(status="blocked", reason_codes=(reason_code,), detail="")

    def _record(
        self,
        plan_fingerprint: str,
        packet: LiveOrderPacket,
        attempted_at: datetime,
        status: str,
        safe_code: str,
    ) -> LiveAuditRecord:
        record = LiveAuditRecord(
            fingerprint=plan_fingerprint,
            client_order_id=packet.client_order_id,
            side=packet.side.value,
            symbol=packet.symbol,
            quantity=packet.quantity,
            attempted_at=attempted_at,
            status=status,
            safe_code=safe_code,
        )
        self._audit.append(record)
        return record

    # -- 실행 진입점 -------------------------------------------------------

    def execute(
        self,
        request: LiveExecutionRequest,
        approval_phrase: str,
        now: datetime | None = None,
    ) -> LiveOutcome:
        """모든 게이트를 호출 시점에 평가한 뒤, 통과 시 transport를 정확히 한 번 호출한다."""
        if not isinstance(request, LiveExecutionRequest):
            raise LiveOrderError("LiveExecutionRequest 객체가 필요합니다.")
        now_utc = _require_utc(now, "now") if now is not None else datetime.now(UTC)

        plan = request.plan
        fingerprint = plan.preview_fingerprint
        packet = build_live_packet(plan)

        with self._lock:
            if self._attempted.get(fingerprint, False):
                return self._blocked("ALREADY_ATTEMPTED")

            transport = self._transport
            missing = _missing_gates(
                request,
                approval_phrase,
                packet,
                now_utc,
                plan,
                transport,
            )
            if missing:
                return self._blocked(missing)
            if transport is None:  # Kept explicit for type narrowing after the gate.
                return self._blocked("TRANSPORT_NOT_AVAILABLE")

            # 원자적으로 시도를 예약한다. 이후 성공/거절/모호 여부와 무관하게 재제출 없음.
            self._attempted[fingerprint] = True

        attempted_at = now_utc
        try:
            raw = transport.submit(packet)
        except LiveOrderTransportError:
            # transport 계약상 이미 정제된 거절이다. 원문 정보는 여기서도 보관하지 않는다.
            self._record(fingerprint, packet, attempted_at, "rejected", "TRANSPORT_SUBMIT_ERROR")
            return LiveOrderRejected(
                status="rejected",
                reason_codes=("TRANSPORT_SUBMIT_ERROR",),
                detail="브로커가 주문을 거절했습니다. 안전 코드만 제공됩니다.",
            )
        except Exception:  # 원문 정보는 절대 보관하지 않는다
            self._record(
                fingerprint, packet, attempted_at, "ambiguous", "AMBIGUOUS_TRANSPORT_ERROR"
            )
            return LiveOrderAmbiguous(
                status="ambiguous",
                safe_code="AMBIGUOUS_TRANSPORT_ERROR",
                detail="제출 결과를 확인할 수 없습니다. 주문 접수 여부를 직접 확인하십시오.",
            )

        accepted = _strict_accepted_response(raw, packet.client_order_id)
        if accepted is None:
            self._record(
                fingerprint, packet, attempted_at, "ambiguous", "AMBIGUOUS_RESPONSE_MISMATCH"
            )
            return LiveOrderAmbiguous(
                status="ambiguous",
                safe_code="AMBIGUOUS_RESPONSE_MISMATCH",
                detail="응답이 요청한 클라이언트 주문 ID와 일치하지 않습니다.",
            )
        order_id, client_order_id = accepted
        self._record(fingerprint, packet, attempted_at, "accepted", "SUBMIT_ACCEPTED")
        return LiveOrderAccepted(
            status="accepted",
            order_id=order_id,
            client_order_id=client_order_id,
            fingerprint=fingerprint,
        )
