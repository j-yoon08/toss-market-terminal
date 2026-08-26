"""v0.8b 동기 주문 전송 계층 테스트.

네트워크는 전부 httpx.MockTransport로 대체되며, 실제 소켓 연결은 차단한다.
실제 브로커 호출·자격증명 접근·OAuth 발급은 존재하지 않는다.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from toss_market_terminal import live_order as lo
from toss_market_terminal import order_preview as op
from toss_market_terminal import order_transport as ot

ACCESS_TOKEN = "test-access-token-not-a-real-secret"
ACCOUNT_SEQ = 7
RAW_ACCOUNT_NO = "50123456701"

LIMIT_KEYS = {
    "clientOrderId",
    "symbol",
    "side",
    "orderType",
    "quantity",
    "timeInForce",
    "confirmHighValueOrder",
    "price",
}
MARKET_KEYS = LIMIT_KEYS - {"price"}
FROZEN_NOW = datetime(2026, 8, 26, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 공용 픽스처/빌더
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _block_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 모듈의 모든 테스트에서 실제 소켓 연결을 하드 차단한다."""

    def _blocked(self: socket.socket, address: object) -> None:
        raise AssertionError("real network access is forbidden in transport tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


def krw_limit_preview() -> op.OrderPreview:
    return op.build_preview(
        **{
            "account_no": RAW_ACCOUNT_NO,
            "account_seq": ACCOUNT_SEQ,
            "symbol": "005930",
            "side": op.OrderSide.BUY,
            "order_type": op.OrderType.LIMIT,
            "quantity": "1",
            "limit_price": "50000",
            "reference_last_price": "50000",
            "holding_quantity": "0",
            "cash_buying_power": "500000",
        }
    )


def usd_market_preview() -> op.OrderPreview:
    return op.build_preview(
        **{
            "account_no": RAW_ACCOUNT_NO,
            "account_seq": ACCOUNT_SEQ,
            "symbol": "AAPL",
            "side": op.OrderSide.SELL,
            "order_type": op.OrderType.MARKET,
            "quantity": "2",
            "limit_price": None,
            "reference_last_price": "49",
            "holding_quantity": "10",
            "cash_buying_power": "150",
        }
    )


def packet_for(preview: op.OrderPreview) -> lo.LiveOrderPacket:
    return lo.build_live_packet(lo.create_live_plan(preview, FROZEN_NOW))


class Recorder:
    """요청을 기록하는 MockTransport 핸들러 어댑터."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responder = responder
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.responder(request)


def make_transport(recorder: Recorder) -> tuple[ot.TossOrderTransport, httpx.Client]:
    injected = httpx.Client(
        transport=httpx.MockTransport(recorder),
        base_url=ot.API_BASE_URL,
        follow_redirects=False,
        trust_env=False,
    )
    transport = ot.TossOrderTransport(
        access_token=ACCESS_TOKEN, account_seq=ACCOUNT_SEQ, client=injected
    )
    return transport, injected


def ok_response(request: httpx.Request) -> httpx.Response:
    coid = json.loads(request.content)["clientOrderId"]
    return httpx.Response(200, json={"result": {"orderId": "ORD-777", "clientOrderId": coid}})


# ---------------------------------------------------------------------------
# 정확한 요청: 경로·메서드·헤더·본문
# ---------------------------------------------------------------------------


def test_limit_post_exact_path_method_headers_and_body() -> None:
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        result = transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()

    packet = packet_for(krw_limit_preview())
    assert set(result.keys()) == {"order_id", "client_order_id"}
    assert result["order_id"] == "ORD-777"
    assert result["client_order_id"] == packet.client_order_id

    # 정확히 한 번의 POST, 고정 경로
    assert len(recorder.calls) == 1
    request = recorder.calls[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/orders"
    assert str(request.url) == ot.API_BASE_URL + "/api/v1/orders"
    # 헤더: Bearer 토큰 + 계좌 시퀀스 + JSON
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["X-Tossinvest-Account"] == str(ACCOUNT_SEQ)
    assert request.headers["Content-Type"].startswith("application/json")

    body = json.loads(request.content)
    assert set(body.keys()) == LIMIT_KEYS
    assert body == {
        "clientOrderId": packet.client_order_id,
        "symbol": "005930",
        "side": "BUY",
        "orderType": "LIMIT",
        "quantity": "1",
        "timeInForce": "DAY",
        "confirmHighValueOrder": False,
        "price": "50000",
    }


def test_market_post_omits_price_and_matches_packet() -> None:
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        result = transport.submit(packet_for(usd_market_preview()))
    finally:
        injected.close()

    assert result["order_id"] == "ORD-777"
    assert result["client_order_id"] == packet_for(usd_market_preview()).client_order_id
    assert set(result.keys()) == {"order_id", "client_order_id"}
    request = recorder.calls[0]
    assert len(recorder.calls) == 1
    body = json.loads(request.content)
    assert set(body.keys()) == MARKET_KEYS
    assert "price" not in body
    packet = packet_for(usd_market_preview())
    assert body == {
        "clientOrderId": packet.client_order_id,
        "symbol": "AAPL",
        "side": "SELL",
        "orderType": "MARKET",
        "quantity": "2",
        "timeInForce": "DAY",
        "confirmHighValueOrder": False,
    }


def test_fractional_us_market_sell_preserves_decimal_string() -> None:
    preview = op.build_preview(
        account_no=RAW_ACCOUNT_NO,
        account_seq=ACCOUNT_SEQ,
        symbol="AAPL",
        side=op.OrderSide.SELL,
        order_type=op.OrderType.MARKET,
        quantity="0.123456",
        limit_price=None,
        reference_last_price="49",
        holding_quantity="10",
        cash_buying_power="150",
    )
    packet = packet_for(preview)
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        transport.submit(packet)
    finally:
        injected.close()

    assert json.loads(recorder.calls[0].content)["quantity"] == "0.123456"


def test_accepted_result_rejects_noncanonical_flat_top_level_shape() -> None:
    def flat(request: httpx.Request) -> httpx.Response:
        coid = json.loads(request.content)["clientOrderId"]
        return httpx.Response(200, json={"orderId": "ORD-1", "clientOrderId": coid})

    recorder = Recorder(flat)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.AmbiguousOrderOutcomeError) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()
    assert excinfo.value.args[0] == "AMBIGUOUS_RESPONSE_ENVELOPE"
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# 네트워크 이전 검증 (호출 0회)
# ---------------------------------------------------------------------------


def _mutated_packet(payload_overrides: dict[str, object]) -> lo.LiveOrderPacket:
    base = packet_for(krw_limit_preview())
    payload = dict(base.payload)
    payload.update(payload_overrides)
    return lo.LiveOrderPacket(
        client_order_id=base.client_order_id,
        fingerprint=base.fingerprint,
        symbol=base.symbol,
        side=base.side,
        quantity=base.quantity,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"extras": "forbidden"}, "unknown extra field"),
        ({"orderAmount": 50000}, "amount-based field"),
        pytest.param({"price": None}, "limit without price", id="limit-no-price"),
        ({"orderType": "MARKET"}, "market keeps limit shape"),
    ],
)
def test_invalid_payloads_blocked_before_network(mutation: dict[str, object], reason: str) -> None:
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.OrderTransportConfigError):
            transport.submit(_mutated_packet(mutation))
    finally:
        injected.close()
    assert len(recorder.calls) == 0


def test_market_with_price_blocked_before_network() -> None:
    base = packet_for(usd_market_preview())
    payload = dict(base.payload)
    payload["price"] = "49"
    mutated = lo.LiveOrderPacket(
        client_order_id=base.client_order_id,
        fingerprint=base.fingerprint,
        symbol=base.symbol,
        side=base.side,
        quantity=base.quantity,
        payload=payload,
    )
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.OrderTransportConfigError):
            transport.submit(mutated)
    finally:
        injected.close()
    assert len(recorder.calls) == 0


@pytest.mark.parametrize(
    "mutation",
    [
        {"quantity": 999},
        {"clientOrderId": "tmt-forged"},
        {"symbol": "000000"},
        {"side": "SELL"},
        {"timeInForce": "GTC"},
        {"confirmHighValueOrder": True},
    ],
)
def test_tampered_fields_blocked_before_network(mutation: dict[str, object]) -> None:
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.OrderTransportConfigError):
            transport.submit(_mutated_packet(mutation))
    finally:
        injected.close()
    assert len(recorder.calls) == 0


def test_non_packet_object_rejected_without_network() -> None:
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.OrderTransportConfigError):
            transport.submit({"payload": "not-a-packet"})  # type: ignore[arg-type]
    finally:
        injected.close()
    assert len(recorder.calls) == 0


@pytest.mark.parametrize(
    ("token", "seq"),
    [
        ("", 7),
        ("   ", 7),
        (" tok", 7),
        ("tok ", 7),
        ("x" * (ot.MAX_ACCESS_TOKEN_LENGTH + 1), 7),
        ("tok", 0),
        ("tok", -1),
        ("tok", True),
    ],
)
def test_constructor_validation_fails_closed(token: str, seq: int) -> None:
    with pytest.raises(ot.OrderTransportConfigError):
        ot.TossOrderTransport(access_token=token, account_seq=seq)


def test_injected_client_must_be_httpx_client() -> None:
    with pytest.raises(ot.OrderTransportConfigError):
        ot.TossOrderTransport(
            ACCESS_TOKEN,
            ACCOUNT_SEQ,
            client="not-a-client",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 상태별 결과: 거절 / 모호 / 단일 호출
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
def test_reject_statuses_raise_sanitized_transport_error(status: int) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": f"BROKER_{status}", "msg": "원문"}})

    recorder = Recorder(reject)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(lo.LiveOrderTransportError) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()

    assert excinfo.value.safe_code == f"BROKER_{status}"
    assert "원문" not in str(excinfo.value)
    assert ACCESS_TOKEN not in str(excinfo.value)
    assert len(recorder.calls) == 1


def test_broker_error_code_is_sanitized_and_body_never_leaks() -> None:
    raw_code = 'ORDER.4001 <script>&x="leak"</script>'

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text=f'{{"error":{{"code":"{raw_code}"}}}}')

    recorder = Recorder(reject)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(lo.LiveOrderTransportError) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()

    safe = excinfo.value.safe_code
    assert set(safe) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    assert "." not in safe and "<" not in safe and '"' not in safe and " " not in safe
    assert "leak" not in str(excinfo.value)
    assert len(recorder.calls) == 1


def test_non_json_reject_body_still_maps_to_http_fallback_code() -> None:
    recorder = Recorder(lambda request: httpx.Response(429, text="<html>gateway</html>"))
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(lo.LiveOrderTransportError) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()
    assert excinfo.value.safe_code == "HTTP_429"
    assert "gateway" not in str(excinfo.value)
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("status", [301, 302, 500, 502, 503, 599])
def test_redirect_and_5xx_are_ambiguous_single_call(status: int) -> None:
    recorder = Recorder(lambda request: httpx.Response(status))
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.AmbiguousOrderOutcomeError):
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()
    assert len(recorder.calls) == 1


def test_timeout_and_connect_errors_are_ambiguous_single_call() -> None:
    for exc in (
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
    ):

        def broken(request: httpx.Request, _exc: Exception = exc) -> httpx.Response:
            raise _exc

        recorder = Recorder(broken)
        transport, injected = make_transport(recorder)
        try:
            with pytest.raises(ot.AmbiguousOrderOutcomeError) as excinfo:
                transport.submit(packet_for(krw_limit_preview()))
        finally:
            injected.close()
        assert excinfo.value.args[0] == "AMBIGUOUS_TRANSPORT_FAILURE"
        assert str(exc) not in str(excinfo.value)
        assert len(recorder.calls) == 1


def test_200_with_invalid_json_is_ambiguous_single_call() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, text="{not-json"))
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.AmbiguousOrderOutcomeError) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()
    assert excinfo.value.args[0] == "AMBIGUOUS_RESPONSE_UNREADABLE"
    assert "{not-json" not in str(excinfo.value)
    assert len(recorder.calls) == 1


def test_200_envelope_violations_are_ambiguous_single_call() -> None:
    cases = [
        lambda r: httpx.Response(200, json={"unexpected": 1}),
        lambda r: httpx.Response(200, json={"result": {"orderId": "", "clientOrderId": "tmt-x"}}),
        lambda r: httpx.Response(200, json={"result": {"orderId": 123}}),
    ]
    for responder in cases:
        coid = packet_for(krw_limit_preview()).client_order_id

        recorder = Recorder(responder)
        transport, injected = make_transport(recorder)
        try:
            with pytest.raises(ot.AmbiguousOrderOutcomeError) as excinfo:
                transport.submit(packet_for(krw_limit_preview()))
        finally:
            injected.close()
        assert excinfo.value.args[0].startswith("AMBIGUOUS_RESPONSE_")
        assert coid not in str(excinfo.value)
        assert len(recorder.calls) == 1


def test_200_client_order_id_mismatch_is_ambiguous_single_call() -> None:
    def mismatch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"result": {"orderId": "ORD-9", "clientOrderId": "other-id"}}
        )

    recorder = Recorder(mismatch)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(ot.AmbiguousOrderOutcomeError) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()
    assert excinfo.value.args[0] == "AMBIGUOUS_RESPONSE_MISMATCH"
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# 비밀 유출 방지: repr / 예외 문자열
# ---------------------------------------------------------------------------


def test_repr_redacts_token_and_account() -> None:
    transport, injected = make_transport(Recorder(ok_response))
    try:
        text = repr(transport)
    finally:
        injected.close()
    assert ACCESS_TOKEN not in text
    assert str(ACCOUNT_SEQ) not in text
    assert "<redacted>" in text


def test_no_secret_leaks_in_any_exception_text() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "DUP",
                    "message": f"token={ACCESS_TOKEN} seq={ACCOUNT_SEQ}",
                }
            },
        )

    recorder = Recorder(reject)
    transport, injected = make_transport(recorder)
    try:
        with pytest.raises(Exception) as excinfo:
            transport.submit(packet_for(krw_limit_preview()))
    finally:
        injected.close()
    rendered = str(excinfo.value)
    assert ACCESS_TOKEN not in rendered
    assert ACCOUNT_SEQ.__str__() not in rendered
    assert f"seq={ACCOUNT_SEQ}" not in rendered


# ---------------------------------------------------------------------------
# 클라이언트 소유권 / 컨텍스트 매니저
# ---------------------------------------------------------------------------


def test_owned_client_is_created_and_closed_by_context_manager() -> None:
    with ot.TossOrderTransport(ACCESS_TOKEN, ACCOUNT_SEQ) as transport:
        assert transport is not None
        assert repr(transport).endswith("client=owned)")
    # 자체 클라이언트는 close 후 닫힌다. 내부 클라이언트에 직접 접근해 확인.


def test_close_closes_owned_client_only_once_shape() -> None:
    transport = ot.TossOrderTransport(ACCESS_TOKEN, ACCOUNT_SEQ)
    transport.close()
    transport.close()  # 재호출해도 안전해야 한다


def test_injected_client_survives_transport_close() -> None:
    recorder = Recorder(ok_response)
    transport, injected = make_transport(recorder)
    transport.close()
    assert injected.is_closed is False
    # 주입된 클라이언트는 여전히 사용 가능해야 한다(소유자가 닫는다).
    result = transport.submit(packet_for(krw_limit_preview()))
    injected.close()
    assert injected.is_closed is True
    assert result["order_id"] == "ORD-777"


# ---------------------------------------------------------------------------
# 실행기 통합: accepted / rejected / ambiguous, 재시도 없음
# ---------------------------------------------------------------------------


def _executor_setup(recorder: Recorder) -> tuple[lo.ManualLiveOrderExecutor, lo.LiveOrderPlan]:
    _, injected = make_transport(recorder)
    transport: lo.LiveOrderTransport = ot.TossOrderTransport(
        access_token=ACCESS_TOKEN, account_seq=ACCOUNT_SEQ, client=injected
    )
    plan = lo.create_live_plan(krw_limit_preview(), FROZEN_NOW)
    return lo.ManualLiveOrderExecutor(transport), plan


def _gated_request(plan: lo.LiveOrderPlan) -> tuple[lo.LiveExecutionRequest, str]:
    packet = lo.build_live_packet(plan)
    phrase = lo.live_approval_phrase(packet)
    request = lo.LiveExecutionRequest(
        plan=plan,
        execute=True,
        acknowledge_final_approval=True,
        interactive_session=True,
    )
    return request, phrase


def test_executor_accepted_via_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(lo.MANUAL_LIVE_ENV_KEY, lo.MANUAL_LIVE_ENV_VALUE)
    recorder = Recorder(ok_response)
    executor, plan = _executor_setup(recorder)
    request, phrase = _gated_request(plan)

    outcome = executor.execute(request, phrase, now=FROZEN_NOW)

    assert isinstance(outcome, lo.LiveOrderAccepted)
    assert outcome.order_id == "ORD-777"
    assert outcome.client_order_id == lo.client_order_id_for(plan.preview_fingerprint)
    assert outcome.fingerprint == plan.preview_fingerprint
    assert len(recorder.calls) == 1
    statuses = [record.status for record in executor.audit_records()]
    assert statuses == ["accepted"]


def test_executor_rejected_via_broker_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(lo.MANUAL_LIVE_ENV_KEY, lo.MANUAL_LIVE_ENV_VALUE)

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "ORDER_DUPLICATE"}})

    recorder = Recorder(reject)
    executor, plan = _executor_setup(recorder)
    request, phrase = _gated_request(plan)

    outcome = executor.execute(request, phrase, now=FROZEN_NOW)

    assert isinstance(outcome, lo.LiveOrderRejected)
    assert outcome.reason_codes == ("TRANSPORT_SUBMIT_ERROR",)
    assert "ORDER_DUPLICATE" not in outcome.detail  # 원문 코드는 detail에 노출되지 않음
    assert len(recorder.calls) == 1
    assert [record.status for record in executor.audit_records()] == ["rejected"]


def test_executor_ambiguous_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(lo.MANUAL_LIVE_ENV_KEY, lo.MANUAL_LIVE_ENV_VALUE)

    state = {"mode": "fail"}

    def flaky(request: httpx.Request) -> httpx.Response:
        if state["mode"] == "fail":
            raise httpx.ConnectError("boom")
        return ok_response(request)

    recorder = Recorder(flaky)
    executor, plan = _executor_setup(recorder)
    request, phrase = _gated_request(plan)

    first = executor.execute(request, phrase, now=FROZEN_NOW)
    assert isinstance(first, lo.LiveOrderAmbiguous)
    assert first.safe_code == "AMBIGUOUS_TRANSPORT_ERROR"
    assert len(recorder.calls) == 1

    # 같은 계획 재실행: 성공 응답으로 바꿔 놓아도 already attempted로 차단(재시도 금지).
    state["mode"] = "ok"
    second = executor.execute(request, phrase, now=FROZEN_NOW)
    assert isinstance(second, lo.LiveOrderRejected)
    assert second.status == "blocked"
    assert second.reason_codes == ("ALREADY_ATTEMPTED",)
    assert len(recorder.calls) == 1  # 여전히 정확히 한 번만 전송됨
