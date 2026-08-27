from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK, InvalidStatus

from .client import TossApiError, TossMarketClient
from .models import Orderbook, Trade, infer_market, normalize_symbol

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
# Bounded silence watchdog: no server frame at all (trade/orderbook/pong/ack)
# within this many seconds forces a reconnect instead of trusting a socket
# that looks open but has gone quiet. Comfortably above the 60s app PING so a
# normal keepalive round-trip never trips it, yet still bounded for quiet
# symbols. Injectable per-instance (see ``TossMarketStream.__init__``) so
# tests can use a much shorter value without patching module state.
DEFAULT_WS_SILENCE_TIMEOUT_SECONDS = 80.0


@dataclass(frozen=True, slots=True)
class StreamStatus:
    state: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TradeEvent:
    symbol: str
    trade: Trade


@dataclass(frozen=True, slots=True)
class OrderbookEvent:
    symbol: str
    orderbook: Orderbook


StreamEvent = StreamStatus | TradeEvent | OrderbookEvent


def subscription_declaration(symbol: str, market: str, request_id: str) -> list[dict[str, Any]]:
    if market not in {"kr", "us"}:
        raise ValueError("시장은 kr 또는 us여야 합니다.")
    symbol = normalize_symbol(symbol)
    return [
        {"id": request_id},
        {"type": f"trade:{market}", "codes": [symbol]},
        {"type": f"orderbook:{market}", "codes": [symbol]},
    ]


def parse_stream_frame(raw: str, symbol: str) -> StreamEvent | None:
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        return StreamStatus("protocol_error", "invalid-json")
    if not isinstance(frame, dict):
        return StreamStatus("protocol_error", "invalid-frame")

    frame_type = frame.get("type")
    if frame_type == "subscriptions":
        rejected = frame.get("rejected")
        if isinstance(rejected, list) and rejected:
            safe_codes = sorted(
                {
                    "".join(
                        ch for ch in str(item.get("code", "rejected")) if ch.isalnum() or ch in "-_"
                    )[:80]
                    or "rejected"
                    for item in rejected
                    if isinstance(item, dict)
                }
            )
            return StreamStatus("rejected", ",".join(safe_codes))
        subscribed = frame.get("subscribed")
        count = len(subscribed) if isinstance(subscribed, list) else 0
        return StreamStatus("subscribed", f"topics={count}")
    if frame_type == "error":
        error = frame.get("error")
        code = error.get("code", "stream-error") if isinstance(error, dict) else "stream-error"
        safe_code = "".join(ch for ch in str(code) if ch.isalnum() or ch in "-_")[:80]
        return StreamStatus("error", safe_code)
    if frame_type == "pong":
        return StreamStatus("pong")
    if frame_type != "message":
        return None

    topic = frame.get("topic")
    data = frame.get("data")
    if not isinstance(topic, str) or not isinstance(data, dict):
        return StreamStatus("protocol_error", "invalid-message")
    if topic.startswith("trade:"):
        return TradeEvent(symbol=symbol, trade=Trade.from_api(data))
    if topic.startswith("orderbook:"):
        return OrderbookEvent(symbol=symbol, orderbook=Orderbook.from_api(data))
    return None


class TossMarketStream:
    def __init__(
        self,
        client: TossMarketClient,
        *,
        silence_timeout_seconds: float = DEFAULT_WS_SILENCE_TIMEOUT_SECONDS,
    ) -> None:
        if silence_timeout_seconds <= 0:
            raise ValueError("무응답 감시 시간은 양수여야 합니다.")
        self._client = client
        self._silence_timeout_seconds = silence_timeout_seconds

    async def events(self, symbol: str, market: str | None = None) -> AsyncIterator[StreamEvent]:
        symbol = normalize_symbol(symbol)
        market = market or infer_market(symbol)
        if market not in {"kr", "us"}:
            raise ValueError("시장은 kr 또는 us여야 합니다.")
        attempt = 0
        while True:
            request_id = f"market-{uuid4().hex[:12]}"
            declaration = subscription_declaration(symbol, market, request_id)
            try:
                token = await self._client.access_token()
                yield StreamStatus("connecting")
                async with connect(
                    WS_URL,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    open_timeout=15,
                    close_timeout=5,
                    max_size=1_000_000,
                    proxy=None,
                ) as websocket:
                    await websocket.send(json.dumps(declaration, separators=(",", ":")))
                    yield StreamStatus("connected")
                    keepalive = asyncio.create_task(self._keepalive(websocket))
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(
                                    websocket.recv(), timeout=self._silence_timeout_seconds
                                )
                            except TimeoutError as exc:
                                # No server frame at all (not even pong) within
                                # the watchdog window: treat exactly like any
                                # other connection failure below -- reconnect
                                # with jittered backoff, then re-subscribe.
                                raise ConnectionError("stream-idle-timeout") from exc
                            except ConnectionClosedOK:
                                break
                            if not isinstance(raw, str):
                                continue
                            event = parse_stream_frame(raw, symbol)
                            if event is not None:
                                yield event
                            if isinstance(event, StreamStatus):
                                if event.state == "subscribed":
                                    # A TCP/WebSocket handshake alone isn't evidence of a
                                    # healthy subscription. Only reset exponential backoff
                                    # after the server acknowledges the requested topics.
                                    attempt = 0
                                if event.state == "error":
                                    raise ConnectionError("stream-error")
                                if event.state == "rejected":
                                    return
                    finally:
                        keepalive.cancel()
                        await asyncio.gather(keepalive, return_exceptions=True)
                raise ConnectionError("stream-closed")
            except asyncio.CancelledError:
                raise
            except TossApiError as exc:
                if exc.status_code in {401, 403}:
                    yield StreamStatus("auth_error", f"http_{exc.status_code}:{exc.code}")
                    return
                attempt += 1
                delay = self._retry_delay(attempt)
                yield StreamStatus("reconnecting", f"retry_in={delay:.1f}s")
                await asyncio.sleep(delay)
            except InvalidStatus as exc:
                status_code = getattr(exc.response, "status_code", 0)
                if status_code in {401, 403}:
                    yield StreamStatus("auth_error", f"websocket_http_{status_code}")
                    return
                attempt += 1
                delay = self._retry_delay(attempt)
                yield StreamStatus("reconnecting", f"retry_in={delay:.1f}s")
                await asyncio.sleep(delay)
            except Exception:
                attempt += 1
                delay = self._retry_delay(attempt)
                yield StreamStatus("reconnecting", f"retry_in={delay:.1f}s")
                await asyncio.sleep(delay)

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(30.0, 2 ** min(attempt - 1, 5)) + random.uniform(0, 0.5)

    @staticmethod
    async def _keepalive(websocket: Any) -> None:
        while True:
            await asyncio.sleep(60)
            await websocket.send("PING")
