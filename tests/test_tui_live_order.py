from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from tests.test_order_ticket import make_context, ticket_snapshot
from tests.test_portfolio import build_snapshot
from toss_market_terminal.live_audit import LiveAuditLog
from toss_market_terminal.live_order import (
    LiveOrderAmbiguous,
    LiveOrderPacket,
    LiveOrderPlan,
    build_live_packet,
    create_live_plan,
    live_approval_phrase,
)
from toss_market_terminal.live_ticket import LiveApprovalScreen
from toss_market_terminal.models import BuyingPower, OpenOrder, OpenOrdersPage
from toss_market_terminal.order_preview import OrderSide, OrderType, build_preview
from toss_market_terminal.tui import TossMarketApp


@pytest.fixture(autouse=True)
def prohibit_external_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(self: socket.socket, *args: object) -> None:
        raise AssertionError("external network attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def preview():
    return build_preview(
        account_no="*******8901",
        account_seq=1,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity="1",
        limit_price="40",
        reference_last_price="40",
        holding_quantity="5",
        cash_buying_power="500",
        market="us",
        currency="USD",
    )


def plan() -> LiveOrderPlan:
    return create_live_plan(preview(), datetime.now(UTC), ttl_seconds=300)


class RecordingTransport:
    def __init__(
        self,
        response: Mapping[str, object] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.response = response or {
            "order_id": "broker-order-id",
            "client_order_id": "",
        }
        self.failure = failure
        self.packets: list[object] = []
        self.factory_args: list[tuple[str, int]] = []
        self.close_count = 0
        self.close_failure = False

    def factory(self, token: str, account_seq: int) -> RecordingTransport:
        self.factory_args.append((token, account_seq))
        return self

    def submit(self, packet: LiveOrderPacket) -> Mapping[str, object]:
        self.packets.append(packet)
        if self.failure is not None:
            raise self.failure
        body = dict(self.response)
        body["client_order_id"] = packet.client_order_id
        return body

    def close(self) -> None:
        self.close_count += 1
        if self.close_failure:
            raise RuntimeError("close provider secret")


class BlockingTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.submit_started = threading.Event()
        self.submit_release = threading.Event()

    def submit(self, packet: LiveOrderPacket) -> Mapping[str, object]:
        self.submit_started.set()
        if not self.submit_release.wait(timeout=5):
            raise TimeoutError("blocked test transport timed out")
        return super().submit(packet)


class BlockingAuditLog(LiveAuditLog):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.append_started = threading.Event()
        self.append_release = threading.Event()

    def append(self, record) -> None:
        self.append_started.set()
        if not self.append_release.wait(timeout=5):
            raise RuntimeError("blocked test audit timed out")
        super().append(record)


def make_live_app(tmp_path: Path, transport: RecordingTransport) -> TossMarketApp:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=ticket_snapshot(),
        connect_live=False,
        manual_live_orders=True,
        live_audit_log=LiveAuditLog(tmp_path / "audit-state" / "audit.jsonl"),
        live_transport_factory=transport.factory,
    )
    context = make_context()

    async def account_loader(symbol: str):
        assert symbol == "AAPL"
        return context

    async def open_loader(account_seq: int, symbol: str) -> OpenOrdersPage:
        assert (account_seq, symbol) == (1, "AAPL")
        return OpenOrdersPage(orders=())

    async def token_loader() -> str:
        return "test-access-token-not-a-real-secret"

    app.account_context_loader = account_loader
    app.open_orders_loader = open_loader
    app.access_token_loader = token_loader
    return app


def open_order(status: str = "PENDING") -> OpenOrder:
    return OpenOrder.from_api(
        {
            "orderId": "existing-order",
            "symbol": "AAPL",
            "side": "BUY",
            "orderType": "LIMIT",
            "status": status,
            "quantity": "1",
            "price": "40",
            "execution": {"filledQuantity": "0"},
        }
    )


async def test_exact_live_gates_submit_once_audit_and_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    app.portfolio_snapshot = build_snapshot()
    account_calls = 0
    original_loader = app.account_context_loader

    async def counted_loader(symbol: str):
        nonlocal account_calls
        account_calls += 1
        assert original_loader is not None
        return await original_loader(symbol)

    app.account_context_loader = counted_loader
    current_plan = plan()
    phrase = live_approval_phrase(build_live_packet(current_plan))

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(current_plan, phrase)

    assert len(transport.packets) == 1
    assert transport.factory_args == [("test-access-token-not-a-real-secret", 1)]
    assert transport.close_count == 1
    assert account_calls == 2  # immediate preflight + read-only reconciliation
    assert app.last_live_outcome is not None
    assert app.last_live_outcome.status == "accepted"
    assert app.last_reconciled_account_context is not None
    assert app.last_reconciled_account_context.symbol == "AAPL"
    assert app.last_reconciled_open_orders == OpenOrdersPage(orders=())
    assert app.live_reconciliation_monotonic is not None
    assert app.live_reconciliation_error is None
    assert app.portfolio_snapshot is not None
    assert app.portfolio_snapshot.open_orders == OpenOrdersPage(orders=())
    assert app.portfolio_snapshot.usd_buying_power.cash_buying_power == Decimal("500")
    assert app.portfolio_stale is True
    assert app.portfolio_error == "주문 후 부분 재조회 · 전체 포트폴리오 동기화 대기"

    audit_path = tmp_path / "audit-state" / "audit.jsonl"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"
    serialized = json.dumps(rows)
    for forbidden in (
        "broker-order-id",
        "test-access-token",
        "accountSeq",
        "approval",
        "raw",
    ):
        assert forbidden not in serialized


async def test_post_submit_reconciliation_updates_full_portfolio_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    app = make_live_app(tmp_path, RecordingTransport())
    reconciled_orders = OpenOrdersPage((open_order(),))
    full_snapshot = replace(build_snapshot(), open_orders=reconciled_orders)
    open_calls = 0
    portfolio_calls = 0

    async def staged_open_loader(account_seq: int, symbol: str) -> OpenOrdersPage:
        nonlocal open_calls
        assert (account_seq, symbol) == (1, "AAPL")
        open_calls += 1
        return OpenOrdersPage(()) if open_calls == 1 else reconciled_orders

    async def portfolio_loader():
        nonlocal portfolio_calls
        portfolio_calls += 1
        return full_snapshot

    app.open_orders_loader = staged_open_loader
    current_plan = plan()
    async with app.run_test(size=(90, 30)):
        # Install after mount so no background polling races this one-shot test.
        app.portfolio_loader = portfolio_loader
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert open_calls == 2
    assert portfolio_calls == 1
    assert app.last_reconciled_open_orders == reconciled_orders
    assert app.portfolio_snapshot == full_snapshot
    assert app.portfolio_stale is False
    assert app.portfolio_error is None
    messages = [notification.message for notification in app._notifications]
    assert any("계좌와 미체결 주문 상태" in message for message in messages)


async def test_close_failure_cannot_override_accepted_result_or_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    transport.close_failure = True
    app = make_live_app(tmp_path, transport)
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert len(transport.packets) == 1
    assert transport.close_count == 1
    assert app.last_live_outcome is not None
    assert app.last_live_outcome.status == "accepted"
    messages = [notification.message for notification in app._notifications]
    assert any("전송 연결 정리" in message for message in messages)
    audit_text = (tmp_path / "audit-state" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"status":"accepted"' in audit_text
    assert "close provider secret" not in audit_text


async def test_cancellation_during_submit_waits_for_and_audits_broker_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = BlockingTransport()
    app = make_live_app(tmp_path, transport)
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        task = asyncio.create_task(
            app._submit_live_plan(
                current_plan, live_approval_phrase(build_live_packet(current_plan))
            )
        )
        assert await asyncio.to_thread(transport.submit_started.wait, 2)
        task.cancel()
        transport.submit_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(transport.packets) == 1
    assert app.last_live_outcome is not None
    assert app.last_live_outcome.status == "accepted"
    audit_text = (tmp_path / "audit-state" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"status":"accepted"' in audit_text


async def test_cancellation_during_audit_append_finishes_durable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    audit_path = tmp_path / "blocking-audit" / "audit.jsonl"
    blocking_audit = BlockingAuditLog(audit_path)
    app.live_audit_log = blocking_audit
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        task = asyncio.create_task(
            app._submit_live_plan(
                current_plan, live_approval_phrase(build_live_packet(current_plan))
            )
        )
        assert await asyncio.to_thread(blocking_audit.append_started.wait, 2)
        task.cancel()
        blocking_audit.append_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(transport.packets) == 1
    assert app.last_live_outcome is not None
    assert app.last_live_outcome.status == "accepted"
    assert '"status":"accepted"' in audit_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("gate", ["wrong-phrase", "env-off", "runtime-off"])
async def test_missing_gate_never_reads_account_or_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    current_plan = plan()
    phrase = live_approval_phrase(build_live_packet(current_plan))
    if gate != "env-off":
        monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    if gate == "wrong-phrase":
        phrase = "wrong"
    if gate == "runtime-off":
        app.manual_live_orders = False

    reads = 0

    async def unexpected_read(symbol: str):
        nonlocal reads
        reads += 1
        raise AssertionError(symbol)

    app.account_context_loader = unexpected_read
    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(current_plan, phrase)

    assert reads == 0
    assert transport.packets == []
    assert not (tmp_path / "audit-state" / "audit.jsonl").exists()


async def test_stale_price_blocks_live_submit_before_account_token_or_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    current_plan = plan()

    reads = 0

    async def unexpected_read(symbol: str):
        nonlocal reads
        reads += 1
        raise AssertionError(symbol)

    token_calls = 0

    async def unexpected_token() -> str:
        nonlocal token_calls
        token_calls += 1
        raise AssertionError("token loader should not be called")

    app.account_context_loader = unexpected_read
    app.access_token_loader = unexpected_token

    async with app.run_test(size=(90, 30)):
        app.last_tick_monotonic = time.monotonic() - 3600.0
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert reads == 0
    assert token_calls == 0
    assert transport.packets == []
    assert not (tmp_path / "audit-state" / "audit.jsonl").exists()


async def test_duplicate_open_order_blocks_before_token_and_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    token_calls = 0

    async def duplicate_loader(account_seq: int, symbol: str) -> OpenOrdersPage:
        return OpenOrdersPage((open_order(),))

    async def token_loader() -> str:
        nonlocal token_calls
        token_calls += 1
        return "unused"

    app.open_orders_loader = duplicate_loader
    app.access_token_loader = token_loader
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert token_calls == 0
    assert transport.packets == []


async def test_audit_preflight_wrong_mode_blocks_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    audit_path = tmp_path / "bad-audit" / "audit.jsonl"
    audit_path.parent.mkdir(mode=0o700)
    audit_path.write_text("", encoding="utf-8")
    audit_path.chmod(0o644)
    app.live_audit_log = LiveAuditLog(audit_path)
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert transport.packets == []
    assert audit_path.read_text(encoding="utf-8") == ""


async def test_fresh_buying_power_drop_blocks_before_open_orders_and_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    stale = make_context()
    stale = replace(stale, buying_power=BuyingPower(currency="USD", cash_buying_power=Decimal("1")))
    open_calls = 0

    async def low_power_loader(symbol: str):
        return stale

    async def unexpected_open(account_seq: int, symbol: str) -> OpenOrdersPage:
        nonlocal open_calls
        open_calls += 1
        return OpenOrdersPage(())

    app.account_context_loader = low_power_loader
    app.open_orders_loader = unexpected_open
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert open_calls == 0
    assert transport.packets == []


async def test_same_fingerprint_never_retries_after_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport(failure=TimeoutError("provider secret body"))
    app = make_live_app(tmp_path, transport)
    current_plan = plan()
    phrase = live_approval_phrase(build_live_packet(current_plan))

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(current_plan, phrase)
        await app._submit_live_plan(current_plan, phrase)

    assert len(transport.packets) == 1
    assert isinstance(app.last_live_outcome, LiveOrderAmbiguous)
    audit_path = tmp_path / "audit-state" / "audit.jsonl"
    text = audit_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1
    assert "provider secret body" not in text
    assert "ambiguous" in text


async def test_live_mode_opens_compact_enter_confirm_modal_after_paper_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)

    async with app.run_test(size=(90, 30)) as pilot:
        app._on_paper_confirmed(preview(), True)
        await pilot.pause()
        assert type(app.screen).__name__ == "LiveApprovalScreen"
        region = app.screen.query_one("#live-approval-dialog").region
        assert 0 < region.width <= 90 and 0 < region.height <= 30
        shown = app.screen.query_one("#live-approval-details", Static).render().plain
        assert app.screen.packet.fingerprint in shown
        assert len(app.screen.packet.fingerprint) == 64
        assert len(app.screen.query(Input)) == 0
        await pilot.press("escape")
        await pilot.pause()

    assert transport.packets == []


async def test_enter_confirmation_requires_armed_screen_then_uses_full_executor_phrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    app = make_live_app(tmp_path, RecordingTransport())
    current_plan = plan()
    results: list[str | None] = []

    async with app.run_test(size=(90, 30)) as pilot:
        screen = LiveApprovalScreen(current_plan)
        app.push_screen(screen, results.append)
        await pilot.pause()

        # An Enter carried over/repeated from the PAPER screen cannot submit.
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen
        assert results == []
        assert screen._armed is False

        # Once the review cooldown completes, one fresh Enter submits.
        screen._arm_after_review()
        await pilot.press("enter")
        await pilot.pause()

    assert results == [live_approval_phrase(build_live_packet(current_plan))]


async def test_repeated_early_enter_stays_blocked_until_review_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    app = make_live_app(tmp_path, RecordingTransport())
    current_plan = plan()
    results: list[str | None] = []

    async with app.run_test(size=(90, 30)) as pilot:
        monkeypatch.setattr("toss_market_terminal.live_ticket.REVIEW_ARM_SECONDS", 60.0)
        screen = LiveApprovalScreen(current_plan)
        app.push_screen(screen, results.append)
        await pilot.pause()
        assert screen._arm_timer is not None
        screen._arm_timer.stop()

        class ResetCounter:
            resets = 0

            def reset(self) -> None:
                self.resets += 1

            def stop(self) -> None:
                pass

        counter = ResetCounter()
        screen._arm_timer = counter  # type: ignore[assignment]
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen
        assert results == []
        assert screen._finished is False
        assert screen._armed is False
        assert counter.resets == 2

        # Trigger the same callback the cooldown timer would invoke, then require
        # a new Enter. This avoids wall-clock flakes under a loaded full suite.
        screen._arm_after_review()
        assert screen._armed is True
        await pilot.press("enter")
        await pilot.pause()

    assert results == [live_approval_phrase(build_live_packet(current_plan))]


async def test_live_runtime_flag_with_env_off_never_opens_approval_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", raising=False)
    app = make_live_app(tmp_path, RecordingTransport())

    async with app.run_test(size=(90, 30)) as pilot:
        app._on_paper_confirmed(preview(), True)
        await pilot.pause()
        assert type(app.screen).__name__ != "LiveApprovalScreen"
        assert app.last_paper_preview is not None
        messages = [notification.message for notification in app._notifications]
        assert any("환경 게이트" in message for message in messages)
