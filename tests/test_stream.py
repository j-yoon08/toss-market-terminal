from __future__ import annotations

from toss_market_terminal.client import TossApiError
from toss_market_terminal.stream import (
    OrderbookEvent,
    StreamStatus,
    TossMarketStream,
    TradeEvent,
    infer_market,
    parse_stream_frame,
    subscription_declaration,
)


def test_market_inference() -> None:
    assert infer_market("005930") == "kr"
    assert infer_market("AAPL") == "us"


def test_declaration_is_read_only_market_data() -> None:
    declaration = subscription_declaration("AAPL", "us", "req-1")
    assert declaration == [
        {"id": "req-1"},
        {"type": "trade:us", "codes": ["AAPL"]},
        {"type": "orderbook:us", "codes": ["AAPL"]},
    ]
    assert "personal:order" not in str(declaration)


def test_parse_ack_trade_and_orderbook() -> None:
    ack = parse_stream_frame(
        '{"type":"subscriptions","subscribed":["trade:us:AAPL","orderbook:us:AAPL"],"rejected":[]}',
        "AAPL",
    )
    trade = parse_stream_frame(
        '{"type":"message","topic":"trade:us:AAPL","data":{"price":"10.2","volume":"3","timestamp":"2026-01-01T00:00:00Z","currency":"USD"}}',
        "AAPL",
    )
    orderbook = parse_stream_frame(
        '{"type":"message","topic":"orderbook:us:AAPL","data":{"timestamp":null,"currency":"USD","asks":[],"bids":[]}}',
        "AAPL",
    )
    assert ack == StreamStatus("subscribed", "topics=2")
    assert isinstance(trade, TradeEvent)
    assert isinstance(orderbook, OrderbookEvent)


async def test_auth_error_is_reported_once_without_retry() -> None:
    class AuthFailClient:
        async def access_token(self) -> str:
            raise TossApiError(401, "invalid_client")

    stream = TossMarketStream(AuthFailClient())  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")
    assert await anext(events) == StreamStatus("auth_error", "http_401:invalid_client")
    try:
        await anext(events)
    except StopAsyncIteration:
        pass
    else:
        raise AssertionError("인증 오류 후 스트림이 종료되어야 합니다.")


def test_rejected_subscription_is_explicit() -> None:
    rejected = parse_stream_frame(
        '{"type":"subscriptions","subscribed":[],"rejected":'
        '[{"target":"trade:us:AAPL","code":"stock-not-found","message":"no"}]}',
        "AAPL",
    )
    assert rejected == StreamStatus("rejected", "stock-not-found")
