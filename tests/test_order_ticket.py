"""v0.7b Textual PAPER 주문 미리보기 티켓 UX 테스트.

경계:
  * 외부 네트워크 금지: socket.socket.connect를 몽키패치해 외부 접속 시 실패.
  * 주문 전송 경로 없음: 어떤 POST/mutation도 만들지 않으며, 가짜 클라이언트는
    읽기 전용 account_context만 제공·기록한다.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from tests.helpers import sample_snapshot
from toss_market_terminal import tui as tui_module
from toss_market_terminal.models import (
    Account,
    AccountContext,
    BuyingPower,
    Cost,
    DailyProfitLoss,
    HoldingsItem,
    MarketValue,
    Price,
    ProfitLoss,
    Trade,
)
from toss_market_terminal.order_preview import (
    SAFETY_POLICY_VERSION,
    OrderPreviewError,
    OrderSide,
    build_preview,
)
from toss_market_terminal.tui import TossMarketApp

RAW_ACCOUNT_NO = "50123456701"
MASKED_ACCOUNT_NO = "*******8901"


@pytest.fixture(autouse=True)
def prohibit_external_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트 전체에서 socket connect를 차단해 외부 네트워크 호출을 금지한다."""

    def _no_connect(self: socket.socket, *args: object) -> None:
        raise AssertionError("테스트 중 외부 네트워크 연결은 금지됩니다")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)


def make_context(symbol: str = "AAPL", currency: str = "USD") -> AccountContext:
    """보유 5주 / 매수가능 500의 읽기 전용 계좌 컨텍스트(시세 40 USD)."""
    holding = HoldingsItem(
        symbol=symbol,
        name="Apple",
        market_country="US",
        currency=currency,
        quantity=Decimal("5"),
        last_price=Decimal("40"),
        average_purchase_price=Decimal("35"),
        market_value=MarketValue(
            purchase_amount=Decimal("500"),
            amount=Decimal("550"),
            amount_after_cost=Decimal("548"),
        ),
        profit_loss=ProfitLoss(
            amount=Decimal("50"),
            amount_after_cost=Decimal("48"),
            rate=Decimal("0.10"),
            rate_after_cost=Decimal("0.096"),
        ),
        daily_profit_loss=DailyProfitLoss(amount=Decimal("2"), rate=Decimal("0.004")),
        cost=Cost(commission=Decimal("2"), tax=None),
    )
    return AccountContext(
        scope="account_read_only",
        order_endpoints_called=False,
        account=Account(
            account_seq=1, account_type="BROKERAGE", masked_account_no=MASKED_ACCOUNT_NO
        ),
        symbol=symbol,
        holding=holding,
        holding_quantity=holding.quantity,
        buying_power=BuyingPower(currency=currency, cash_buying_power=Decimal("500")),
    )


class FakeAccountClient:
    """읽기 전용 account_context만 제공하는 가짜 클라이언트.

    POST/mutation 성격의 메서드를 하나도 정의하지 않고, 호출 심볼만 기록한다.
    """

    def __init__(self, context: AccountContext) -> None:
        self.context = context
        self.calls: list[str] = []

    async def account_context(self, symbol: str) -> AccountContext:
        self.calls.append(symbol)
        return self.context


class ExplodingAccountClient(FakeAccountClient):
    async def account_context(self, symbol: str) -> AccountContext:
        self.calls.append(symbol)
        raise RuntimeError(f"secret-provider-body-for-{symbol}")


class SlowLoader:
    """응답이 늦게 오는 상황을 재현하는 account_context 로더."""

    def __init__(self, context: AccountContext) -> None:
        self.context = context
        self.calls: list[str] = []

    async def __call__(self, symbol: str) -> AccountContext:
        self.calls.append(symbol)
        await asyncio.sleep(0.05)
        return self.context


def ticket_snapshot():
    """마운트 시 current_price=40 USD가 되는 스냅샷(안전 상한 100 USD 내 통과)."""
    base = sample_snapshot(fresh_price=True)
    return replace(
        base,
        price=Price("AAPL", Decimal("40"), "USD", base.price.timestamp),
        trades=(Trade(Decimal("40"), Decimal("2"), base.price.timestamp, "USD"),),
    )


def make_app(
    context: AccountContext | None,
    tmp_path: Path,
    *,
    client: FakeAccountClient | None = None,
    loader: SlowLoader | None = None,
) -> TossMarketApp:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=ticket_snapshot(),
        connect_live=False,
    )
    if loader is not None:
        app.account_context_loader = loader.__call__
    elif client is not None:
        app.account_context_loader = client.account_context
    elif context is not None:
        app.account_context_loader = FakeAccountClient(context).account_context
    return app


async def press_flow(pilot, keys: str) -> None:
    for key in keys:
        await pilot.press(key)
        await pilot.pause()


# ---------------------------------------------------------------------------
# 바인딩과 도움말/푸터 문구
# ---------------------------------------------------------------------------


def test_bindings_and_help_describe_paper_preview_only() -> None:
    keys = [binding.key for binding in TossMarketApp.BINDINGS]
    assert keys.count("b") == 1
    assert keys.count("s") == 1
    help_text = "\n".join(f"{key} {desc}" for key, desc in tui_module.HELP_LINES)
    assert "PAPER 매수 미리보기" in help_text
    assert "PAPER 매도 미리보기" in help_text
    assert "주문 전송 없음" in help_text
    for binding in TossMarketApp.BINDINGS:
        key = getattr(binding, "key", None)
        description = getattr(binding, "description", "")
        if key == "b":
            assert "PAPER" in description
        if key == "s":
            assert "PAPER" in description


async def test_b_and_s_open_ticket_fetching_captured_symbol(tmp_path: Path) -> None:
    context = make_context()
    fake = FakeAccountClient(context)
    app = make_app(None, tmp_path, client=fake)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        ticket = app.screen
        assert type(ticket).__name__ == "OrderTicketScreen"
        assert fake.calls == ["AAPL"]  # 캡처한 현재 심볼로만 조회
        summary = ticket.query_one("#ticket-summary", Static).render().plain
        assert "매수(BUY)" in summary
        assert "AAPL" in summary
        banner = ticket.query_one("#ticket-banner", Static).render().plain
        assert banner == "PAPER_PREVIEW · 실제 주문 전송 없음"
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()
        summary = app.screen.query_one("#ticket-summary", Static).render().plain
        assert "매도(SELL)" in summary
        assert len(fake.calls) == 2
        await pilot.press("escape")


# ---------------------------------------------------------------------------
# 티켓 입력 흐름(LIMIT/MARKET)
# ---------------------------------------------------------------------------


async def test_limit_flow_quantity_then_price_builds_preview(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        ticket = app.screen
        qty = ticket.query_one("#ticket-quantity", Input)
        price = ticket.query_one("#ticket-price", Input)

        assert price.disabled is False
        assert price.value == "40"  # 기본 LIMIT은 현재가

        qty.focus()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert price.has_focus  # 수량 Enter는 지정가로 이동(LIMIT)

        await pilot.press("enter")  # 지정가 그대로 미리보기 작성
        await pilot.pause()

        confirm = app.screen
        assert type(confirm).__name__ == "OrderConfirmScreen"
        body = confirm.query_one("#confirm-body", Static).render().plain
        assert "매수(BUY)" in body
        assert "AAPL" in body
        assert "지정가(LIMIT)" in body
        assert "지정가 40 USD" in body
        assert "수량: 1" in body
        assert "추정 금액: 40 USD" in body
        assert MASKED_ACCOUNT_NO in body
        assert RAW_ACCOUNT_NO not in body
        banner = confirm.query_one("#confirm-banner", Static).render().plain
        assert banner == "PAPER_PREVIEW · 실제 주문 전송 없음"
        assert (
            "Enter PAPER 미리보기 확정" in confirm.query_one("#confirm-help", Static).render().plain
        )


async def test_market_toggle_disables_limit_and_builds_directly(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        ticket = app.screen
        qty = ticket.query_one("#ticket-quantity", Input)
        price = ticket.query_one("#ticket-price", Input)

        qty.focus()
        await pilot.press("m")
        await pilot.pause()
        label = ticket.query_one("#ticket-ordertype", Static).render().plain
        assert "시장가(MARKET)" in label
        assert "비활성" in label
        assert price.disabled is True
        assert price.value == ""

        await pilot.press("2")
        await pilot.pause()
        await pilot.press("enter")  # MARKET에서는 수량 Enter가 바로 미리보기 작성
        await pilot.pause()

        confirm = app.screen
        assert type(confirm).__name__ == "OrderConfirmScreen"
        body = confirm.query_one("#confirm-body", Static).render().plain
        assert "시장가(MARKET)" in body
        assert "참고가 40 USD" in body
        assert "추정 금액: 80 USD" in body

        # m 토글 복귀: 다시 지정가 모드로 돌아가면 입력이 살아난다
        # (확인 화면이라 여기서는 티켓 화면 동작을 직접 검증하지 않는다)


async def test_m_key_inside_input_never_types_the_letter(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        qty = app.screen.query_one("#ticket-quantity", Input)
        qty.focus()
        await pilot.press("m")
        await pilot.pause()
        assert qty.value == ""  # m은 글자 입력이 아니라 유형 전환


# ---------------------------------------------------------------------------
# 확인·취소 경계
# ---------------------------------------------------------------------------


async def test_confirm_stores_only_last_paper_preview_and_notifies(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert app.screen.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 → 확인 모달
        await pilot.pause()
        assert type(app.screen).__name__ == "OrderConfirmScreen"
        await pilot.press("enter")  # 확인 모달에서 로컬 확정
        await pilot.pause()

        preview = app.last_paper_preview
        assert preview is not None
        assert preview.mode == "PAPER_PREVIEW"
        assert preview.order_endpoint_called is False
        assert preview.automatic_retry is False
        assert preview.manual_approval_only is True
        assert preview.intent.masked_account_no == MASKED_ACCOUNT_NO
        messages = [n.message for n in app._notifications]
        assert any(
            "PAPER PREVIEW" in message and "전송되지 않았습니다" in message for message in messages
        )


async def test_cancel_at_confirm_stores_nothing(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert type(app.screen).__name__ == "OrderConfirmScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.last_paper_preview is None


async def test_esc_at_ticket_cancels_without_building(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert type(app.screen).__name__ != "OrderConfirmScreen"
        assert app.last_paper_preview is None


async def test_modal_keys_do_not_reach_underlying_app(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        watchlist_count_before = len(app.watchlist_symbols)
        chart_mode_before = app.chart_mode
        connection_state_before = app.connection_state

        await pilot.press("b")
        await pilot.pause()
        # 모달 위에서 앱 바인딩으로 쓰이는 키를 눌러도 아래 화면이 반응하지 않는다
        for key in ("a", "r", "c", "j", "k", "s"):
            await pilot.press(key)
            await pilot.pause()
            assert type(app.screen).__name__ == "OrderTicketScreen"

        assert len(app.watchlist_symbols) == watchlist_count_before
        assert app.chart_mode == chart_mode_before
        assert app.connection_state == connection_state_before
        assert not app._exit

        # 티켓 안에서 m은 유형 전환일 뿐, 차트 모드를 바꾸지 않는다
        qty = app.screen.query_one("#ticket-quantity", Input)
        qty.focus()
        chart_mode_before_toggle = app.chart_mode
        await pilot.press("m")
        await pilot.pause()
        assert app.chart_mode == chart_mode_before_toggle
        assert "시장가(MARKET)" in app.screen.query_one("#ticket-ordertype", Static).render().plain


# ---------------------------------------------------------------------------
# fail-closed 케이스
# ---------------------------------------------------------------------------


async def test_oversell_never_reaches_confirmation(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)  # 보유 5주
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        qty = app.screen.query_one("#ticket-quantity", Input)
        qty.focus()
        await pilot.press("9")
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert app.screen.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 → 보유 수량 검증에서 거부
        await pilot.pause()
        error = app.screen.query_one("#ticket-error", Static).render().plain
        assert "보유 수량을 초과" in error
        assert type(app.screen).__name__ == "OrderTicketScreen"


async def test_overbudget_buy_is_rejected_in_modal(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)  # 매수가능 500 USD, 안전 상한 100 USD
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        qty = app.screen.query_one("#ticket-quantity", Input)
        qty.focus()
        await pilot.press("9")
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert app.screen.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 → 예산/상한 검증에서 거부
        await pilot.pause()
        error = app.screen.query_one("#ticket-error", Static).render().plain
        assert ("매수가능금액을 초과" in error) or ("안전 상한" in error)
        assert type(app.screen).__name__ == "OrderTicketScreen"


async def test_invalid_decimal_shows_concise_korean_error(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        qty = app.screen.query_one("#ticket-quantity", Input)
        qty.focus()
        await pilot.press("0")  # 수량 0 거부
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert app.screen.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 시 수량 재검증에서 거부
        await pilot.pause()
        error = app.screen.query_one("#ticket-error", Static).render().plain
        assert "양수" in error
        assert type(app.screen).__name__ == "OrderTicketScreen"


async def test_missing_current_price_blocks_ticket_open(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        app.current_price = None
        app.current_currency = ""
        await app._open_paper_ticket(OrderSide.BUY)
        await pilot.pause()
        assert type(app.screen).__name__ != "OrderTicketScreen"
        assert app.last_paper_preview is None


async def test_stale_price_blocks_ticket_open_before_account_loader(tmp_path: Path) -> None:
    context = make_context()
    fake = FakeAccountClient(context)
    app = make_app(None, tmp_path, client=fake)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        app.last_tick_monotonic = tui_module.time.monotonic() - 3600.0
        await app._open_paper_ticket(OrderSide.BUY)
        await pilot.pause()
        assert type(app.screen).__name__ != "OrderTicketScreen"
        assert app.last_paper_preview is None
        assert fake.calls == []  # blocked before any account/network loading
        messages = [n.message for n in app._notifications]
        assert any("신선도" in message for message in messages)


async def test_unsupported_currency_blocks_ticket_open(tmp_path: Path) -> None:
    app = make_app(make_context(currency="EUR"), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        app.current_currency = "EUR"
        await app._open_paper_ticket(OrderSide.BUY)
        await pilot.pause()
        assert type(app.screen).__name__ != "OrderTicketScreen"


async def test_client_failure_is_sanitized_and_opens_nothing(tmp_path: Path) -> None:
    exploding = ExplodingAccountClient(make_context())
    app = make_app(None, tmp_path, client=exploding)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        assert type(app.screen).__name__ != "OrderTicketScreen"
        messages = [n.message for n in app._notifications]
        assert all("secret-provider-body" not in message for message in messages)
        assert any("계좌 정보를 확인할 수 없" in message for message in messages)


async def test_stale_symbol_race_never_builds_confirmation(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        ticket = app.screen
        app.symbol = "MSFT"  # 응답 이후 활성 종목이 바뀐 상황 재현
        qty = ticket.query_one("#ticket-quantity", Input)
        qty.focus()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert ticket.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 → stale 검사로 미리보기 거부
        await pilot.pause()
        error = ticket.query_one("#ticket-error", Static).render().plain
        assert ("종목이 변경" in error) or ("일치하지 않" in error)
        assert type(app.screen).__name__ == "OrderTicketScreen"
        assert app.last_paper_preview is None


async def test_duplicate_open_is_serialized_by_lock(tmp_path: Path) -> None:
    loader = SlowLoader(make_context())
    app = make_app(None, tmp_path, loader=loader)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        app.current_price = Decimal("110")
        app.current_currency = "USD"
        first = asyncio.create_task(app._open_paper_ticket(OrderSide.BUY))
        second = asyncio.create_task(app._open_paper_ticket(OrderSide.BUY))
        await asyncio.gather(first, second)
        await pilot.pause()

        tickets = [
            screen for screen in app.screen_stack if type(screen).__name__ == "OrderTicketScreen"
        ]
        assert len(tickets) == 1
        assert len(loader.calls) == 1


async def test_compact_90x30_ticket_and_confirm_are_visible(tmp_path: Path) -> None:
    app = make_app(make_context(), tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        region = app.screen.query_one("#ticket-dialog").region
        assert 0 < region.width <= 90 and 0 < region.height <= 30
        assert region.width >= 40 and region.height >= 10

        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert app.screen.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 → 확인 모달
        await pilot.pause()
        cregion = app.screen.query_one("#order-confirm-dialog").region
        assert 0 < cregion.width <= 90 and 0 < cregion.height <= 30


# ---------------------------------------------------------------------------
# 네트워크/전송 경계와 프라이버시
# ---------------------------------------------------------------------------


async def test_fake_client_records_only_account_context_calls(tmp_path: Path) -> None:
    fake = FakeAccountClient(make_context())
    app = make_app(None, tmp_path, client=fake)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")  # 수량 제출 → 지정가 입력으로 이동(LIMIT 2단계)
        await pilot.pause()
        assert app.screen.query_one("#ticket-price", Input).has_focus
        await pilot.press("enter")  # 지정가 제출 → 확인 모달
        await pilot.pause()
        assert type(app.screen).__name__ == "OrderConfirmScreen"
        await pilot.press("enter")  # 확인 모달에서 로컬 확정
        await pilot.pause()

    assert set(fake.calls) == {"AAPL"}
    assert app.last_paper_preview is not None
    assert app.last_paper_preview.order_endpoint_called is False


def test_fake_client_has_no_mutation_methods() -> None:
    fake = FakeAccountClient(make_context())
    forbidden = {"post", "put", "patch", "delete", "place_order", "submit_order", "order"}
    public_names = {name.lower() for name in dir(fake) if not name.startswith("_")}
    assert public_names & forbidden == set()
    assert callable(fake.account_context)


def test_sanitize_ticket_error_never_leaks_provider_details() -> None:
    from toss_market_terminal.order_ticket import sanitize_ticket_error

    text = sanitize_ticket_error(RuntimeError("Bearer token abc.def.ghi leaked"))
    assert "token" not in text.lower()
    assert "abc" not in text
    assert (
        sanitize_ticket_error(OrderPreviewError("수량 값은 양수여야 합니다."))
        == "수량 값은 양수여야 합니다."
    )


def test_fingerprint_prefix_from_pure_domain_is_available() -> None:
    preview = build_preview(
        account_no=RAW_ACCOUNT_NO,
        account_seq=1,
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        quantity="1",
        reference_last_price="100",
        holding_quantity="5",
        cash_buying_power="500",
        limit_price="100",  # 기존 안전 상한(100 USD)과 동일: 상한 초과가 아니다
    )
    assert len(preview.fingerprint) == 64
    assert preview.approval_phrase.endswith(preview.fingerprint[:8])
    assert SAFETY_POLICY_VERSION.startswith("0.7a")
