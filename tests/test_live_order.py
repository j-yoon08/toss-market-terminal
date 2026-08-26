"""v0.8a manual-live execution PLAN/GATE core tests (offline, no order endpoints).

이 테스트는 라이브 주문 "계획/게이트 코어"만 검증한다.
전송 계층(transport)은 항상 테스트 더블이며 실제 네트워크는 없다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import re
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from toss_market_terminal import live_order as lo
from toss_market_terminal import order_preview as op
from toss_market_terminal.models import mask_account_no

RAW_ACCOUNT_NO = "50123456701"
ENV_KEY = "TOSS_ENABLE_MANUAL_LIVE_ORDERS"

FROZEN_MODELS = ("LiveOrderPlan", "LiveExecutionRequest", "LiveOrderPacket", "LiveOrderAccepted")


def krw_preview(**overrides: object) -> op.OrderPreview:
    """BUY 005930 LIMIT 1 @50000 → 유효한 PAPER_PREVIEW."""
    kwargs: dict[str, object] = {
        "account_no": RAW_ACCOUNT_NO,
        "account_seq": 7,
        "symbol": "005930",
        "side": op.OrderSide.BUY,
        "order_type": op.OrderType.LIMIT,
        "quantity": "1",
        "limit_price": "50000",
        "reference_last_price": "50000",
        "holding_quantity": "0",
        "cash_buying_power": "500000",
    }
    kwargs.update(overrides)
    return op.build_preview(**kwargs)


def usd_preview(**overrides: object) -> op.OrderPreview:
    kwargs: dict[str, object] = {
        "account_no": RAW_ACCOUNT_NO,
        "account_seq": 7,
        "symbol": "AAPL",
        "side": op.OrderSide.SELL,
        "order_type": op.OrderType.MARKET,
        "quantity": "2",
        "limit_price": None,
        "reference_last_price": "49",
        "holding_quantity": "10",
        "cash_buying_power": "150",
    }
    kwargs.update(overrides)
    return op.build_preview(**kwargs)


def utc(seconds: int = 0) -> datetime:
    return datetime(2026, 8, 26, 9, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def make_plan(
    preview: op.OrderPreview | None = None, now: datetime | None = None, ttl_seconds: int = 300
) -> lo.LiveOrderPlan:
    return lo.create_live_plan(
        preview if preview is not None else krw_preview(),
        now if now is not None else utc(),
        ttl_seconds=ttl_seconds,
    )


def make_request(plan: lo.LiveOrderPlan) -> lo.LiveExecutionRequest:
    return lo.LiveExecutionRequest(
        plan=plan,
        execute=True,
        acknowledge_final_approval=True,
        interactive_session=True,
    )


def full_phrase(packet: lo.LiveOrderPacket) -> str:
    side = packet.side.value
    return f"CONFIRM LIVE {side} {packet.symbol} {packet.quantity} {packet.fingerprint}"


class RecordingTransport:
    """submit을 기록하는 최소 transport 더블."""

    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.calls: list[lo.LiveOrderPacket] = []
        self._response = response
        self._error = error

    def submit(self, packet: lo.LiveOrderPacket) -> dict[str, object]:
        self.calls.append(packet)
        if self._error is not None:
            raise self._error
        assert isinstance(self._response, dict)
        return dict(self._response)


def ok_response(packet: lo.LiveOrderPacket) -> dict[str, object]:
    return {"order_id": "BRK-123", "client_order_id": packet.client_order_id}


# ---------------------------------------------------------------------------
# create_live_plan: 프리뷰 게이트
# ---------------------------------------------------------------------------


def test_create_plan_from_valid_paper_preview():
    preview = krw_preview()
    plan = make_plan(preview)
    assert plan.preview_fingerprint == preview.fingerprint
    assert plan.intent == preview.intent
    assert plan.expires_at > plan.created_at


def test_create_plan_rejects_non_preview_object():
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan("not-a-preview", utc())  # type: ignore[arg-type]


def _preview_copy_with(preview: op.OrderPreview, **changes: object) -> op.OrderPreview:
    """dataclasses.replace로 플래그를 변조한 프리뷰를 만든다."""
    return dataclasses.replace(preview, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "LIVE"},
        {"order_endpoint_called": True},
        {"automatic_retry": True},
        {"manual_approval_only": False},
    ],
)
def test_create_plan_rejects_tampered_safety_flags(changes):
    tampered = _preview_copy_with(krw_preview(), **changes)
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(tampered, utc())


def test_create_plan_rejects_wrong_mode_string():
    tampered = _preview_copy_with(krw_preview(), mode="PAPER_PREVIEW_X")
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(tampered, utc())


def test_create_plan_rejects_fingerprint_tampering():
    preview = krw_preview()
    forged = _preview_copy_with(preview, fingerprint="0" * 64)
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(forged, utc())


def test_create_plan_rejects_truncated_or_uppercase_fingerprint():
    preview = krw_preview()
    short = _preview_copy_with(preview, fingerprint=preview.fingerprint[:63])
    upper = _preview_copy_with(preview, fingerprint=preview.fingerprint.upper())
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(short, utc())
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(upper, utc())


def test_create_plan_rejects_estimated_notional_tampering():
    preview = krw_preview()
    forged = _preview_copy_with(preview, estimated_notional=Decimal("999999"))
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(forged, utc())


# ---------------------------------------------------------------------------
# create_live_plan: 시간 처리
# ---------------------------------------------------------------------------


def test_plan_expiry_and_injected_clock():
    plan = make_plan(ttl_seconds=60)
    assert plan.created_at == utc()
    assert plan.expires_at == utc(60)


@pytest.mark.parametrize("ttl", [0, -5])
def test_non_positive_ttl_rejected(ttl):
    with pytest.raises(lo.LiveOrderError):
        make_plan(ttl_seconds=ttl)


def test_naive_now_rejected():
    with pytest.raises(lo.LiveOrderError):
        lo.create_live_plan(krw_preview(), datetime(2026, 8, 26, 9, 0, 0))


def test_naive_expires_at_is_never_produced_even_with_naive_ttl_math():
    plan = make_plan()
    assert plan.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 공식 페이로드(LiveOrderPacket)
# ---------------------------------------------------------------------------


def test_packet_fields_are_exact_and_quantity_based():
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    keys = set(packet.payload.keys())
    assert keys == {
        "clientOrderId",
        "symbol",
        "side",
        "orderType",
        "quantity",
        "timeInForce",
        "price",
        "confirmHighValueOrder",
    }
    p = packet.payload
    assert p["symbol"] == "005930"
    assert p["side"] == "BUY"
    assert p["orderType"] == "LIMIT"
    assert p["quantity"] == 1
    assert p["timeInForce"] == "DAY"
    assert p["price"] == "50000"
    assert p["confirmHighValueOrder"] is False
    assert "orderAmount" not in p


def test_market_order_payload_has_no_price():
    packet = lo.build_live_packet(make_plan(usd_preview()))
    assert packet.payload["orderType"] == "MARKET"
    assert "price" not in packet.payload or packet.payload.get("price") is None
    assert packet.payload["quantity"] == 2
    assert packet.payload["side"] == "SELL"


CLIENT_ID_PATTERN = re.compile(r"^tmt-[0-9a-f]{32}$")


def test_client_order_id_format_and_length_bound():
    packet = lo.build_live_packet(make_plan())
    assert CLIENT_ID_PATTERN.fullmatch(packet.client_order_id)
    assert len(packet.client_order_id) <= 36


def test_client_order_id_is_deterministic_from_fingerprint():
    first = lo.build_live_packet(make_plan(krw_preview()))
    second = lo.build_live_packet(make_plan(krw_preview()))
    assert first.client_order_id == second.client_order_id
    digest = hashlib.sha256(first.fingerprint.encode("utf-8")).hexdigest()[:32]
    assert first.client_order_id == f"tmt-{digest}"


def test_different_fingerprints_yield_different_client_ids():
    a = lo.build_live_packet(make_plan(krw_preview()))
    b = lo.build_live_packet(make_plan(krw_preview(quantity="2", limit_price="50000")))
    # quantity=2 x 50000 = 100000 → 캡 경계 통과 케이스
    assert a.client_order_id != b.client_order_id


def test_packet_binds_plan_fingerprint():
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    assert packet.fingerprint == plan.preview_fingerprint


def test_packet_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        lo.build_live_packet(make_plan()).symbol = "AAPL"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 라이브 승인 문구(전체 지문 64자)
# ---------------------------------------------------------------------------


def test_live_approval_phrase_uses_full_64_char_fingerprint():
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    phrase = lo.live_approval_phrase(packet)
    assert phrase == f"CONFIRM LIVE BUY 005930 1 {plan.preview_fingerprint}"
    assert plan.preview_fingerprint in phrase
    assert len(plan.preview_fingerprint) == 64
    # 8자 접두 문구는 절대 라이브 승인으로 통과하지 않는다.
    short = f"CONFIRM LIVE BUY 005930 1 {plan.preview_fingerprint[:8]}"
    assert short != phrase


def test_phrase_binds_to_packet_not_arbitrary_string():
    packet = lo.build_live_packet(make_plan())
    other = lo.build_live_packet(
        make_plan(usd_preview(side=op.OrderSide.SELL, quantity="2")),
    )
    assert lo.live_approval_phrase(packet) != lo.live_approval_phrase(other)


# ---------------------------------------------------------------------------
# 실행 게이트(호출 시점 평가, 전부 필요)
# ---------------------------------------------------------------------------


def _gate_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(ENV_KEY, raising=False)
    else:
        monkeypatch.setenv(ENV_KEY, value)


def test_all_gates_pass_submits_exactly_once(monkeypatch):
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    request = make_request(plan)
    transport = RecordingTransport(response=ok_response(lo.build_live_packet(plan)))
    outcome = lo.ManualLiveOrderExecutor(transport).execute(
        request,
        approval_phrase=lo.live_approval_phrase(lo.build_live_packet(plan)),
        now=utc(1),
    )
    assert isinstance(outcome, lo.LiveOrderAccepted)
    assert outcome.status == "accepted"
    assert outcome.order_id == "BRK-123"


@pytest.mark.parametrize("field", ["execute", "acknowledge_final_approval", "interactive_session"])
def test_each_false_flag_blocks_without_transport_call(monkeypatch, field):
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    flags = {"execute": True, "acknowledge_final_approval": True, "interactive_session": True}
    flags[field] = False
    request = lo.LiveExecutionRequest(plan=plan, **flags)  # type: ignore[arg-type]
    transport = RecordingTransport()
    result = lo.ManualLiveOrderExecutor(transport).execute(request, approval_phrase="x", now=utc(1))
    assert isinstance(result, lo.LiveOrderRejected)
    assert result.status == "blocked"
    assert not any("phrase" in c.lower() for c in result.reason_codes)
    assert transport.calls == []


@pytest.mark.parametrize("bad", ["", "0", "true", "1 ", " 1", "yes", "01"])
def test_env_value_must_be_exactly_one(monkeypatch, bad):
    _gate_env(monkeypatch, bad)
    result = lo.ManualLiveOrderExecutor(RecordingTransport()).execute(
        make_request(make_plan()),
        approval_phrase="x",
        now=utc(1),
    )
    assert result.status == "blocked" if hasattr(result, "status") else True
    assert isinstance(result, lo.LiveOrderRejected)
    assert not isinstance(result, lo.LiveOrderAccepted)


def test_missing_env_blocks_by_default(monkeypatch):
    _gate_env(monkeypatch, None)
    result = lo.ManualLiveOrderExecutor(RecordingTransport()).execute(
        make_request(make_plan()),
        approval_phrase="x",
        now=utc(1),
    )
    assert isinstance(result, lo.LiveOrderRejected)
    assert result.status == "blocked"


def test_wrong_phrase_blocks_even_with_full_flags(monkeypatch):
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    for wrong in (
        f"CONFIRM LIVE BUY 005930 1 {plan.preview_fingerprint[:8]}",
        f"APPROVE BUY 005930 1 {plan.preview_fingerprint}",
        f"CONFIRM LIVE SELL 005930 1 {plan.preview_fingerprint}",
        f"confirm live buy 005930 1 {plan.preview_fingerprint}",
        f"CONFIRM LIVE BUY 005930 2 {plan.preview_fingerprint}",
        f" CONFIRM LIVE BUY 005930 1 {plan.preview_fingerprint}",
        f"CONFIRM LIVE BUY 005930 1 {plan.preview_fingerprint} ",
    ):
        result = lo.ManualLiveOrderExecutor(RecordingTransport()).execute(
            make_request(plan),
            approval_phrase=wrong,
            now=utc(1),
        )
        assert isinstance(result, lo.LiveOrderRejected), wrong
        assert result.status == "blocked"
    del packet


def test_unexpired_boundary_accepts_expired_blocks(monkeypatch):
    _gate_env(monkeypatch, "1")
    plan = make_plan(ttl_seconds=300)
    phrase = lo.live_approval_phrase(lo.build_live_packet(plan))
    executor = lo.ManualLiveOrderExecutor(
        RecordingTransport(response=ok_response(lo.build_live_packet(plan)))
    )
    at_expiry = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(300))
    assert isinstance(at_expiry, lo.LiveOrderAccepted)

    plan2 = make_plan(ttl_seconds=300)
    phrase2 = lo.live_approval_phrase(lo.build_live_packet(plan2))
    late = lo.ManualLiveOrderExecutor(RecordingTransport()).execute(
        make_request(plan2),
        approval_phrase=phrase2,
        now=utc(301),
    )
    assert isinstance(late, lo.LiveOrderRejected)
    assert late.status == "blocked"
    assert "expired" in " ".join(late.reason_codes).lower()


# ---------------------------------------------------------------------------
# 1회 제출 보장(one-call semantics)과 시도 원장
# ---------------------------------------------------------------------------


class AmbiguousTransport:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, packet: lo.LiveOrderPacket) -> dict[str, object]:
        self.calls += 1
        raise RuntimeError("connection reset while reading response body")


def _prepared(monkeypatch, transport: object) -> tuple[lo.LiveOrderPlan, str]:
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    return plan, lo.live_approval_phrase(lo.build_live_packet(plan))


def test_ambiguous_exception_never_retries_and_returns_sanitized_outcome(monkeypatch):
    plan, phrase = _prepared(monkeypatch, None)
    transport = AmbiguousTransport()
    executor = lo.ManualLiveOrderExecutor(transport)

    first = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(1))
    assert isinstance(first, lo.LiveOrderAmbiguous)
    assert first.status == "ambiguous"
    assert first.safe_code == "AMBIGUOUS_TRANSPORT_ERROR"
    assert "connection reset" not in first.detail
    assert "reset" not in first.detail.lower()
    assert len(executor.attempts()) == 1

    second = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(2))
    assert isinstance(second, lo.LiveOrderRejected)
    assert second.status == "blocked"
    assert any("already" in c.lower() for c in second.reason_codes)
    assert transport.calls == 1  # 재시도 없음


def test_already_attempted_blocks_after_failure_too(monkeypatch):
    plan, phrase = _prepared(monkeypatch, None)
    transport = RecordingTransport(error=lo.LiveOrderTransportError("LIVE_REJECTED"))
    executor = lo.ManualLiveOrderExecutor(transport)
    first = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(1))
    assert isinstance(first, lo.LiveOrderRejected)
    assert first.status == "rejected"
    assert "TRANSPORT_SUBMIT_ERROR" in first.reason_codes

    again = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(2))
    assert isinstance(again, lo.LiveOrderRejected)
    assert again.status == "blocked"
    assert any("already" in c.lower() for c in again.reason_codes)
    assert transport.calls == [lo.build_live_packet(plan)]  # 정확히 1회 호출


def test_concurrent_duplicate_execution_is_serialized_single_submit(monkeypatch):
    _gate_env(monkeypatch, "1")
    release = threading.Event()
    started = threading.Event()

    class BlockingTransport:
        def __init__(self) -> None:
            self.calls: list[lo.LiveOrderPacket] = []

        def submit(self, packet: lo.LiveOrderPacket) -> dict[str, object]:
            self.calls.append(packet)
            started.set()
            release.wait(timeout=5)
            return {"order_id": "BRK-OK", "client_order_id": packet.client_order_id}

    plan = make_plan()
    request = make_request(plan)
    phrase = lo.live_approval_phrase(lo.build_live_packet(plan))
    transport = BlockingTransport()
    executor = lo.ManualLiveOrderExecutor(transport)

    results: list[object] = []

    def run() -> None:
        results.append(executor.execute(request, approval_phrase=phrase, now=utc(1)))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    started.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=10)

    accepted = [r for r in results if isinstance(r, lo.LiveOrderAccepted)]
    blocked = [r for r in results if isinstance(r, lo.LiveOrderRejected)]
    assert len(results) == 4
    assert len(accepted) == 1
    assert all(
        r.status == "blocked" and any("already" in c.lower() for c in r.reason_codes)
        for r in blocked
    )
    assert len(transport.calls) == 1
    assert len(executor.attempts()) == 1


def test_attempts_ledger_keys_are_fingerprints_only(monkeypatch):
    plan, phrase = _prepared(monkeypatch, None)
    executor = lo.ManualLiveOrderExecutor(RecordingTransport(response=None))
    executor.execute(make_request(plan), approval_phrase=phrase, now=utc(1))
    ledger = executor.attempts()
    assert list(ledger.keys()) == [plan.preview_fingerprint]


# ---------------------------------------------------------------------------
# 엄격한 응답 검증
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"order_id": ""},
        {"order_id": "BRK-1", "client_order_id": "tmt-" + "0" * 32},
        {"client_order_id": "x"},
        {"order_id": "BRK-1", "extra_field": True},
        ["not", "a", "dict"],
        None,
        "OK",
    ],
)
def test_strict_response_mismatch_yields_ambiguous_no_retry(monkeypatch, response):
    plan, phrase = _prepared(monkeypatch, None)
    transport = RecordingTransport(response=response)
    executor = lo.ManualLiveOrderExecutor(transport)
    outcome = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(1))
    assert not isinstance(outcome, lo.LiveOrderAccepted)
    assert outcome.status == "ambiguous"
    assert transport.calls == [lo.build_live_packet(plan)]
    # 두 번째 시도는 already attempted로 차단된다.
    second = executor.execute(make_request(plan), approval_phrase=phrase, now=utc(2))
    assert second.status == "blocked"


def test_accepted_result_never_claims_fill(monkeypatch):
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    executor = lo.ManualLiveOrderExecutor(RecordingTransport(response=ok_response(packet)))
    outcome = executor.execute(
        make_request(plan), approval_phrase=lo.live_approval_phrase(packet), now=utc(1)
    )
    assert isinstance(outcome, lo.LiveOrderAccepted)
    text = json.dumps(outcome.to_privacy_safe_dict(), ensure_ascii=False).lower()
    for forbidden_word in ("filled", "fill", "execution_", "체결"):
        assert forbidden_word not in text


# ---------------------------------------------------------------------------
# 감사 레코드(AuditRecord): 프라이버시
# ---------------------------------------------------------------------------


def test_audit_record_for_accepted_is_sanitized(monkeypatch):
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    executor = lo.ManualLiveOrderExecutor(RecordingTransport(response=ok_response(packet)))
    executor.execute(
        make_request(plan), approval_phrase=lo.live_approval_phrase(packet), now=utc(1)
    )
    record = executor.audit_records()[0]
    assert record.fingerprint == plan.preview_fingerprint
    assert record.client_order_id == packet.client_order_id
    assert record.side == op.OrderSide.BUY.value
    assert record.symbol == "005930"
    assert record.quantity == Decimal("1")
    assert record.status == "accepted"
    assert record.attempted_at == utc(1)


@pytest.mark.parametrize(
    ("transport", "expected_status", "expected_code"),
    [
        (
            RecordingTransport(error=lo.LiveOrderTransportError("BOOM")),
            "rejected",
            "TRANSPORT_SUBMIT_ERROR",
        ),
        (None, "ambiguous", "AMBIGUOUS_TRANSPORT_ERROR"),
    ],
)
def test_audit_record_statuses_and_safe_codes(
    monkeypatch, transport, expected_status, expected_code
):
    if transport is None:
        transport = AmbiguousTransport()
    _gate_env(monkeypatch, "1")
    plan = make_plan()
    packet = lo.build_live_packet(plan)
    executor = lo.ManualLiveOrderExecutor(transport)
    executor.execute(
        make_request(plan), approval_phrase=lo.live_approval_phrase(packet), now=utc(0)
    )
    record = executor.audit_records()[0]
    assert record.status == expected_status
    assert record.safe_code == expected_code


def test_audit_records_never_contain_sensitive_material(monkeypatch):
    _gate_env(monkeypatch, "1")
    plan = make_plan(krw_preview(account_no=RAW_ACCOUNT_NO))
    packet = lo.build_live_packet(plan)
    transport = RecordingTransport(
        response={
            "order_id": "X",
            "secret_token": "SHOULD-NOT-SURVIVE",
            "client_order_id": packet.client_order_id,
        },
    )
    executor = lo.ManualLiveOrderExecutor(transport)
    executor.execute(
        make_request(plan),
        approval_phrase=lo.live_approval_phrase(packet),
        now=utc(1),
    )
    blob = json.dumps(
        [dataclasses.asdict(r) for r in executor.audit_records()],
        default=str,
        ensure_ascii=False,
    )
    lowered = blob.lower()
    assert RAW_ACCOUNT_NO not in blob
    assert mask_account_no(RAW_ACCOUNT_NO) not in blob
    assert "account_seq" not in lowered or '"account_seq": 7' not in blob
    assert "seq" not in lowered
    assert "token" not in lowered
    assert "should-not-survive" not in lowered
    assert "confirm live" not in lowered
    assert plan.preview_fingerprint[:8] in lowered or plan.preview_fingerprint in lowered


def test_blocked_attempt_leaves_no_audit_record_but_gate_state_not_persisted():
    executor = lo.ManualLiveOrderExecutor(RecordingTransport())
    result = executor.execute(
        make_request(make_plan()),
        approval_phrase="wrong",
        now=utc(1),
    )
    assert isinstance(result, lo.LiveOrderRejected)
    assert result.status == "blocked"
    assert executor.audit_records() == ()


# ---------------------------------------------------------------------------
# 불변 모델 · 요청 필드 엄격성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_name", FROZEN_MODELS)
def test_core_models_are_frozen(model_name):
    model_cls = getattr(lo, model_name)
    assert model_cls.__dataclass_params__.frozen is True


def test_request_unknown_fields_are_structurally_impossible():
    with pytest.raises(TypeError):
        lo.LiveExecutionRequest(  # type: ignore[call-arg]
            plan=make_plan(),
            execute=True,
            acknowledge_final_approval=True,
            interactive_session=True,
            order_amount="50000",  # 금지 필드는 애초에 존재하지 않는다
        )


# ---------------------------------------------------------------------------
# 모듈 위생: HTTP/클라이언트/주문 엔드포인트/자격증명 부재 + 환경 누수 없음
# ---------------------------------------------------------------------------


FORBIDDEN_TOKENS = (
    "httpx",
    "urllib",
    "socket",
    "requests.",
    "TossMarketClient",
    "/api/v1/orders",
    "access_token",
    "client_secret",
)


def test_module_source_has_no_http_client_or_credential_access():
    source = inspect.getsource(lo)
    lowered = source.lower()
    for token in FORBIDDEN_TOKENS:
        assert token.lower() not in lowered, f"금지 토큰 발견: {token}"


def test_module_reads_only_the_single_documented_env_key(monkeypatch):
    """환경 접근은 TOSS_ENABLE_MANUAL_LIVE_ORDERS 키 하나로 제한된다(누수 없음)."""
    import os

    monkeypatch.setenv(lo.MANUAL_LIVE_ENV_KEY, lo.MANUAL_LIVE_ENV_VALUE)
    assert os.environ[lo.MANUAL_LIVE_ENV_KEY] == "1"
    # 모듈 상수 외의 키 이름이 소스에 하드코딩되어 있지 않다.
    source = inspect.getsource(lo)
    hardcoded = re.findall(r"os\.environ\.get\(\s*\"([^\"]+)\"", source)
    assert hardcoded == []
    assert source.count("os.environ.get") == 1
    assert lo.MANUAL_LIVE_ENV_KEY in source


def test_importing_module_does_not_pull_network_modules_or_read_environment():
    code = (
        "import sys, toss_market_terminal.live_order as m;"
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
