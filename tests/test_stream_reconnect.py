from __future__ import annotations

import asyncio
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus

from toss_market_terminal.stream import StreamStatus, TossMarketStream

_REAL_SLEEP = asyncio.sleep

# Sentinel meaning "the fake socket never delivers another frame"; used to
# provoke the real (tiny, test-scoped) silence watchdog timeout.
SILENCE = object()


class FakeWebSocket:
    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        item = self._frames.pop(0)
        if item is SILENCE:
            await asyncio.Event().wait()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeConnection:
    def __init__(
        self, *, websocket: FakeWebSocket | None = None, open_error: BaseException | None = None
    ) -> None:
        self.websocket = websocket
        self.open_error = open_error

    async def __aenter__(self) -> FakeWebSocket:
        if self.open_error is not None:
            raise self.open_error
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class FakeConnect:
    def __init__(self, connections: list[FakeConnection]) -> None:
        self._connections = list(connections)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeConnection:
        self.calls.append({"url": url, **kwargs})
        return self._connections.pop(0)


class FakeClient:
    def __init__(self, token: str = "fake-token") -> None:
        self._token = token
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self._token


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


async def _never_wake(_websocket: Any) -> None:
    await asyncio.Event().wait()


def _patch_stream(
    monkeypatch: pytest.MonkeyPatch, module: Any, connections: list[FakeConnection]
) -> tuple[FakeConnect, FakeClient, list[float]]:
    fake_connect = FakeConnect(connections)
    monkeypatch.setattr(module, "connect", fake_connect)
    monkeypatch.setattr(module.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(module.TossMarketStream, "_keepalive", staticmethod(_never_wake))

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    return fake_connect, FakeClient(), sleep_calls


def test_constructor_rejects_nonpositive_timeout() -> None:
    client = FakeClient()
    with pytest.raises(ValueError):
        TossMarketStream(client, silence_timeout_seconds=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TossMarketStream(client, silence_timeout_seconds=-1)  # type: ignore[arg-type]


async def test_idle_timeout_reconnects_with_backoff_and_resubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toss_market_terminal.stream as stream_module

    ack = '{"type":"subscriptions","subscribed":["trade:us:AAPL"],"rejected":[]}'
    pong = '{"type":"pong"}'
    trade = (
        '{"type":"message","topic":"trade:us:AAPL",'
        '"data":{"price":"10.2","volume":"3","timestamp":"2026-01-01T00:00:00Z","currency":"USD"}}'
    )

    conn1 = FakeConnection(websocket=FakeWebSocket([SILENCE]))
    conn2 = FakeConnection(websocket=FakeWebSocket([ack, pong, trade, SILENCE]))
    fake_connect, client, sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1, conn2])

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")

    reconnecting = await anext(events)
    assert reconnecting == StreamStatus("reconnecting", "retry_in=1.0s")
    assert "stream-idle-timeout" not in reconnecting.detail
    assert "token" not in reconnecting.detail.lower()

    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    assert (await anext(events)).state == "subscribed"
    assert (await anext(events)) == StreamStatus("pong")
    trade_event = await anext(events)
    assert trade_event.symbol == "AAPL"

    second_reconnect = await anext(events)
    assert second_reconnect == StreamStatus("reconnecting", "retry_in=1.0s")

    await events.aclose()

    # The generator yields `reconnecting` before entering that attempt's
    # backoff sleep. Closing at the yield point therefore observes only the
    # first reconnect's single sleep and proves no duplicate sleeps occurred.
    assert sleep_calls == [1.0]
    assert len(conn1.websocket.sent) == 1
    assert len(conn2.websocket.sent) == 1
    assert client.calls == 2
    assert len(fake_connect.calls) == 2
    assert all(call["proxy"] is None for call in fake_connect.calls)


async def test_immediate_closes_increase_backoff_until_subscription_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toss_market_terminal.stream as stream_module

    conn1 = FakeConnection(websocket=FakeWebSocket([ConnectionClosedOK(None, None)]))
    conn2 = FakeConnection(websocket=FakeWebSocket([ConnectionClosedOK(None, None)]))
    conn3 = FakeConnection(websocket=FakeWebSocket([SILENCE]))
    fake_connect, client, sleep_calls = _patch_stream(
        monkeypatch, stream_module, [conn1, conn2, conn3]
    )

    events = TossMarketStream(client, silence_timeout_seconds=0.02).events(  # type: ignore[arg-type]
        "AAPL", "us"
    )
    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    assert await anext(events) == StreamStatus("reconnecting", "retry_in=1.0s")
    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    assert await anext(events) == StreamStatus("reconnecting", "retry_in=2.0s")
    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    await events.aclose()

    assert sleep_calls == [1.0, 2.0]
    assert len(fake_connect.calls) == 3
    assert all(call["proxy"] is None for call in fake_connect.calls)


async def test_normal_close_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    import toss_market_terminal.stream as stream_module

    conn1 = FakeConnection(websocket=FakeWebSocket([ConnectionClosedOK(None, None)]))
    conn2 = FakeConnection(websocket=FakeWebSocket([SILENCE]))
    fake_connect, client, sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1, conn2])

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    assert (await anext(events)).state == "reconnecting"
    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")

    await events.aclose()
    assert len(fake_connect.calls) == 2
    assert sleep_calls == [1.0]


async def test_abnormal_close_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    import toss_market_terminal.stream as stream_module

    conn1 = FakeConnection(websocket=FakeWebSocket([ConnectionClosedError(None, None)]))
    conn2 = FakeConnection(websocket=FakeWebSocket([SILENCE]))
    fake_connect, client, sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1, conn2])

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    assert (await anext(events)).state == "reconnecting"
    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")

    await events.aclose()
    assert len(fake_connect.calls) == 2
    assert sleep_calls == [1.0]


@pytest.mark.parametrize("status_code", [401, 403])
async def test_websocket_auth_error_is_terminal(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    import toss_market_terminal.stream as stream_module

    conn1 = FakeConnection(open_error=InvalidStatus(FakeResponse(status_code)))  # type: ignore[arg-type]
    fake_connect, client, sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1])

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    status = await anext(events)
    assert status == StreamStatus("auth_error", f"websocket_http_{status_code}")

    with pytest.raises(StopAsyncIteration):
        await anext(events)

    assert len(fake_connect.calls) == 1
    assert sleep_calls == []


async def test_rejected_subscription_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    import toss_market_terminal.stream as stream_module

    rejected = (
        '{"type":"subscriptions","subscribed":[],'
        '"rejected":[{"target":"trade:us:AAPL","code":"stock-not-found","message":"no"}]}'
    )
    conn1 = FakeConnection(websocket=FakeWebSocket([rejected]))
    fake_connect, client, sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1])

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")
    status = await anext(events)
    assert status.state == "rejected"

    with pytest.raises(StopAsyncIteration):
        await anext(events)

    assert len(fake_connect.calls) == 1
    assert sleep_calls == []


async def test_cancellation_during_recv_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    import toss_market_terminal.stream as stream_module

    conn1 = FakeConnection(websocket=FakeWebSocket([asyncio.CancelledError()]))
    _fake_connect, client, sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1])

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    assert await anext(events) == StreamStatus("connected")

    with pytest.raises(asyncio.CancelledError):
        await anext(events)

    assert sleep_calls == []


async def test_cancellation_during_backoff_sleep_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toss_market_terminal.stream as stream_module

    conn1 = FakeConnection(open_error=RuntimeError("boom"))
    fake_connect, client, _sleep_calls = _patch_stream(monkeypatch, stream_module, [conn1])

    async def cancelling_sleep(_delay: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(stream_module.asyncio, "sleep", cancelling_sleep)

    stream = TossMarketStream(client, silence_timeout_seconds=0.02)  # type: ignore[arg-type]
    events = stream.events("AAPL", "us")

    assert await anext(events) == StreamStatus("connecting")
    status = await anext(events)
    assert status.state == "reconnecting"

    with pytest.raises(asyncio.CancelledError):
        await anext(events)

    assert len(fake_connect.calls) == 1
