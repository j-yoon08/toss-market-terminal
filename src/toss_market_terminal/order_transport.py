"""v0.8b 동기 Toss 주문 전송 계층(수동 라이브 전용).

v0.8a의 ``LiveOrderPacket``을 브로커 주문 엔드포인트로 정확히 한 번 전송하는
동기 transport 구현만 담당한다. TUI·CLI에는 아직 연결되어 있지 않다.

경계 선언:
  * 자격증명(Credentials)을 import하지 않고 OAuth 발급을 수행하지 않는다.
    액세스 토큰은 호출자가 미리 발급한 것을 생성자로 주입받는다.
  * 요청 경로·메서드는 모듈 상수로 고정되며 호출자가 지정할 수 없다.
  * 제출은 정확히 한 번의 POST로 끝난다. 어떤 경우에도 재시도하지 않는다.
  * 원문 응답·오류 본문·토큰·계좌 값은 예외와 repr 어디에도 담지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Self

import httpx

from toss_market_terminal.live_order import LiveOrderPacket, LiveOrderTransportError

API_BASE_URL = "https://openapi.tossinvest.com"
ORDER_SUBMIT_PATH = "/api/v1/orders"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ACCESS_TOKEN_LENGTH = 4096

# 브로커가 명확히 거절한 상태만 LiveOrderTransportError로 변환한다.
_REJECT_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 422, 429})

# 수량 기반 공식 페이로드 스키마. LIMIT 한정 price 외 추가 필드는 존재할 수 없다.
_BASE_PAYLOAD_KEYS = frozenset(
    {
        "clientOrderId",
        "symbol",
        "side",
        "orderType",
        "quantity",
        "timeInForce",
        "confirmHighValueOrder",
    }
)
_LIMIT_ONLY_KEYS = frozenset({"price"})
_ALLOWED_TIF = frozenset({"DAY"})

_CODE_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_PRICE_TEXT_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_MAX_SAFE_CODE_LEN = 80


class OrderTransportConfigError(ValueError):
    """네트워크 호출 전에 차단되는 설정·패킷 검증 실패(fail-closed)."""


class AmbiguousOrderOutcomeError(RuntimeError):
    """제출 결과를 알 수 없음(타임아웃·연결 실패·5xx·리다이렉트·응답 판독 불가).

    절대 재시도해서는 안 되며, 실행기는 이 예외를 모호 실패로 기록해야 한다.
    """


def _sanitize_error_code(value: object, fallback: str) -> str:
    """브로커 오류 코드에서 안전한 문자만 남긴다. 원문 본문은 절대 노출하지 않는다."""
    if not isinstance(value, str):
        return fallback
    cleaned = _CODE_SANITIZE_RE.sub("_", value).strip("_")[:_MAX_SAFE_CODE_LEN]
    return cleaned or fallback


def _broker_rejection(response: httpx.Response) -> LiveOrderTransportError:
    """거절 상태를 정제된 짧은 코드만 담은 LiveOrderTransportError로 바꾼다."""
    fallback = f"HTTP_{response.status_code}"
    body: object = None
    try:
        body = response.json()
    except ValueError:
        body = None
    candidate: object = None
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            candidate = error.get("code")
        if candidate is None:
            candidate = body.get("code")
    return LiveOrderTransportError(_sanitize_error_code(candidate, fallback))


def _strict_accepted_result(body: object, expected_client_order_id: str) -> dict[str, object]:
    """200 응답에서 orderId·일치하는 clientOrderId가 있을 때만 정제 결과를 반환한다."""
    if not isinstance(body, Mapping):
        raise AmbiguousOrderOutcomeError("AMBIGUOUS_RESPONSE_ENVELOPE")
    result = body.get("result")
    if not isinstance(result, Mapping):
        raise AmbiguousOrderOutcomeError("AMBIGUOUS_RESPONSE_ENVELOPE")
    order_id = result.get("orderId")
    client_order_id = result.get("clientOrderId")
    if not isinstance(order_id, str) or not order_id.strip():
        raise AmbiguousOrderOutcomeError("AMBIGUOUS_RESPONSE_ENVELOPE")
    if client_order_id != expected_client_order_id:
        raise AmbiguousOrderOutcomeError("AMBIGUOUS_RESPONSE_MISMATCH")
    return {"order_id": order_id, "client_order_id": expected_client_order_id}


def _validated_submit_body(packet: LiveOrderPacket) -> dict[str, object]:
    """패킷 페이로드를 전송 전에 엄격하게 재검증한다(변조 시 네트워크 없이 차단)."""
    payload = packet.payload
    if not isinstance(payload, Mapping):
        raise OrderTransportConfigError("패킷 페이로드가 매핑이 아닙니다.")

    order_type = payload.get("orderType")
    if order_type not in {"LIMIT", "MARKET"}:
        raise OrderTransportConfigError("orderType은 LIMIT 또는 MARKET이어야 합니다.")

    expected_keys = set(_BASE_PAYLOAD_KEYS)
    if order_type == "LIMIT":
        expected_keys |= set(_LIMIT_ONLY_KEYS)
    if set(payload.keys()) != expected_keys:
        raise OrderTransportConfigError(
            "페이로드 필드 집합이 공식 수량 기반 스키마와 일치하지 않습니다."
        )
    # 정확성 강조용 이중 확인(집합 비교가 이미 보장하지만 의도를 명시한다).
    if order_type == "LIMIT" and "price" not in payload:
        raise OrderTransportConfigError("LIMIT 주문에는 price가 필요합니다.")
    if order_type == "MARKET" and "price" in payload:
        raise OrderTransportConfigError("MARKET 주문에는 price가 있으면 안 됩니다.")

    if payload.get("clientOrderId") != packet.client_order_id:
        raise OrderTransportConfigError("clientOrderId가 패킷과 일치하지 않습니다.")
    if payload.get("symbol") != packet.symbol:
        raise OrderTransportConfigError("symbol이 패킷과 일치하지 않습니다.")
    if payload.get("side") != packet.side.value:
        raise OrderTransportConfigError("side가 패킷과 일치하지 않습니다.")
    if payload.get("timeInForce") not in _ALLOWED_TIF:
        raise OrderTransportConfigError("timeInForce는 DAY여야 합니다.")
    if payload.get("confirmHighValueOrder") is not False:
        raise OrderTransportConfigError("confirmHighValueOrder는 false여야 합니다.")

    quantity = payload.get("quantity")
    if (
        not isinstance(quantity, str)
        or len(quantity) > 30
        or not _PRICE_TEXT_RE.fullmatch(quantity)
    ):
        raise OrderTransportConfigError("quantity는 30자 이하의 decimal 문자열이어야 합니다.")
    parsed_quantity = Decimal(quantity)
    if not parsed_quantity.is_finite() or parsed_quantity <= 0:
        raise OrderTransportConfigError("quantity는 유한한 양수여야 합니다.")
    if parsed_quantity != packet.quantity:
        raise OrderTransportConfigError("quantity가 패킷 수량과 일치하지 않습니다.")

    if order_type == "LIMIT":
        price_text = payload.get("price")
        if not isinstance(price_text, str) or not _PRICE_TEXT_RE.fullmatch(price_text):
            raise OrderTransportConfigError("price는 숫자 텍스트여야 합니다.")
        price = Decimal(price_text)
        if not price.is_finite() or price <= 0:
            raise OrderTransportConfigError("price는 양수여야 합니다.")

    return dict(payload)


class TossOrderTransport:
    """패킷을 고정 경로로 정확히 한 번 POST하는 동기 transport.

    * ``live_order.LiveOrderTransport`` 프로토콜을 구조적으로 만족한다.
    * 자체 클라이언트를 만들 수도 있지만, 주입된 클라이언트는 절대 닫지 않는다.
    """

    def __init__(
        self,
        access_token: str,
        account_seq: int,
        client: httpx.Client | None = None,
    ) -> None:
        if (
            not isinstance(access_token, str)
            or not access_token
            or access_token != access_token.strip()
            or len(access_token) > MAX_ACCESS_TOKEN_LENGTH
        ):
            raise OrderTransportConfigError(
                "access_token은 공백 없는 1~4096자 문자열이어야 합니다."
            )
        if isinstance(account_seq, bool) or not isinstance(account_seq, int) or account_seq <= 0:
            raise OrderTransportConfigError("account_seq는 양의 정수여야 합니다.")
        if client is not None and not isinstance(client, httpx.Client):
            raise OrderTransportConfigError("client는 httpx.Client 인스턴스여야 합니다.")

        self._access_token = access_token
        self._account_seq = account_seq
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                base_url=API_BASE_URL,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
            )
        )

    def __repr__(self) -> str:
        client_kind = "injected" if not self._owns_client else "owned"
        return (
            "TossOrderTransport("
            "access_token='<redacted>', "
            "account_seq='<redacted>', "
            f"client={client_kind})"
        )

    def submit(self, packet: LiveOrderPacket) -> Mapping[str, object]:
        """검증된 패킷을 정확히 한 번 POST하고 정제된 결과만 반환한다."""
        if not isinstance(packet, LiveOrderPacket):
            raise OrderTransportConfigError("LiveOrderPacket 객체만 전송할 수 있습니다.")
        body = _validated_submit_body(packet)  # 네트워크 호출 전에 실패-closed 검증

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "X-Tossinvest-Account": str(self._account_seq),
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(ORDER_SUBMIT_PATH, json=body, headers=headers)
        except httpx.HTTPError:
            # 타임아웃·연결 실패 등: 접수 여부를 알 수 없으므로 재시도 없이 모호 실패.
            raise AmbiguousOrderOutcomeError("AMBIGUOUS_TRANSPORT_FAILURE") from None

        if response.status_code == 200:
            try:
                payload_json = response.json()
            except ValueError:
                raise AmbiguousOrderOutcomeError("AMBIGUOUS_RESPONSE_UNREADABLE") from None
            return _strict_accepted_result(payload_json, packet.client_order_id)

        if response.status_code in _REJECT_STATUS_CODES:
            raise _broker_rejection(response)

        # 리다이렉트·5xx·기타 상태: 결과를 알 수 없으므로 모호 실패(재시도 금지).
        raise AmbiguousOrderOutcomeError("AMBIGUOUS_HTTP_STATUS")

    def close(self) -> None:
        """자체 생성한 클라이언트만 닫는다. 주입된 클라이언트는 소유자가 닫는다."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
