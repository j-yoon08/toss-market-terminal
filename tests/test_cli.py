from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import ClassVar

import pytest

from toss_market_terminal import cli
from toss_market_terminal.cli import build_parser, main
from toss_market_terminal.models import (
    Account,
    AccountContext,
    BuyingPower,
    Cost,
    DailyProfitLoss,
    HoldingsItem,
    MarketValue,
    ProfitLoss,
)
from toss_market_terminal.settings import DEFAULT_SETTINGS_PATH


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests must never open a real connection (includes account probes)."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external network attempted in CLI tests")

    monkeypatch.setattr("socket.socket.connect", _blocked)


def sample_context() -> AccountContext:
    item = HoldingsItem(
        symbol="AAPL",
        name="Apple Inc.",
        market_country="US",
        currency="USD",
        quantity=Decimal("10"),
        last_price=Decimal("178.5"),
        average_purchase_price=Decimal("155.3"),
        market_value=MarketValue(
            purchase_amount=Decimal("1553"),
            amount=Decimal("1785"),
            amount_after_cost=Decimal("1771.43"),
        ),
        profit_loss=ProfitLoss(
            amount=Decimal("232"),
            amount_after_cost=Decimal("218.43"),
            rate=Decimal("0.1494"),
            rate_after_cost=Decimal("0.1406"),
        ),
        daily_profit_loss=DailyProfitLoss(amount=Decimal("25"), rate=Decimal("0.0142")),
        cost=Cost(commission=Decimal("3.57"), tax=Decimal("10")),
    )
    return AccountContext(
        scope="account_read_only",
        order_endpoints_called=False,
        account=Account(account_seq=7, account_type="BROKERAGE", masked_account_no="*******8901"),
        symbol="AAPL",
        holding=item,
        holding_quantity=Decimal("10"),
        buying_power=BuyingPower(currency="USD", cash_buying_power=Decimal("3500.5")),
    )


class FakeAccountClient:
    """Stands in for TossMarketClient: records calls, returns canned context."""

    last_kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, credentials: object) -> None:
        self.credentials = credentials

    async def __aenter__(self) -> FakeAccountClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def account_context(self, symbol: str, account_seq: int | None = None) -> AccountContext:
        FakeAccountClient.last_kwargs = {"symbol": symbol, "account_seq": account_seq}
        return sample_context()


def install_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAccountClient.last_kwargs = {}
    monkeypatch.setattr(cli, "TossMarketClient", FakeAccountClient)
    monkeypatch.setattr(
        cli, "Credentials", type("C", (), {"load": staticmethod(lambda path: object())})
    )


def test_watchlist_subcommands_parse() -> None:
    parser = build_parser()
    args = parser.parse_args(["watchlist", "list"])
    assert (args.command, args.watchlist_command) == ("watchlist", "list")

    args = parser.parse_args(["--settings", "/tmp/s.json", "watchlist", "add", "aapl"])
    assert args.command == "watchlist"
    assert str(args.settings_path) == "/tmp/s.json"
    assert args.symbol == "aapl"

    args = parser.parse_args(["watchlist", "--settings", "/tmp/t.json", "remove", "005930"])
    assert str(args.settings) == "/tmp/t.json"
    assert args.symbol == "005930"


def test_alert_subcommands_parse() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["alert", "--settings", "/tmp/s.json", "add", "AAPL", "change-below", "-3.5"]
    )
    assert (args.alert_command, args.symbol, args.kind, args.threshold) == (
        "add",
        "AAPL",
        "change-below",
        "-3.5",
    )
    assert str(args.settings) == "/tmp/s.json"
    args = parser.parse_args(["alert", "remove", "A12"])
    assert args.id == "A12"
    args = parser.parse_args(["alert", "list"])
    assert args.alert_command == "list"


def test_watch_and_snapshot_accept_settings_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["--settings", "/tmp/other.json", "watch", "NVDA"])
    assert args.command == "watch"
    assert str(args.settings_path) == "/tmp/other.json"
    args = parser.parse_args(["snapshot", "AAPL"])
    assert str(args.settings_path) == str(DEFAULT_SETTINGS_PATH)


def test_watch_live_orders_flag_is_explicit_and_default_off() -> None:
    parser = build_parser()
    assert parser.parse_args(["watch", "AAPL"]).live_orders is False
    assert parser.parse_args(["watch", "AAPL", "--live-orders"]).live_orders is True


def test_live_shortcut_is_an_explicit_subcommand() -> None:
    args = build_parser().parse_args(["live"])
    assert args.command == "live"
    assert args.symbol is None

    args = build_parser().parse_args(["live", "aapl"])
    assert args.symbol == "aapl"


def test_watch_passes_live_orders_only_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    class FakeApp:
        def __init__(self, symbol: str, credentials: object, **kwargs: object) -> None:
            captured.append({"symbol": symbol, "credentials": credentials, **kwargs})

        def run(self) -> int:
            return 0

    monkeypatch.setattr(cli, "TossMarketApp", FakeApp)
    assert main(["watch", "aapl"]) == 0
    assert main(["watch", "AAPL", "--live-orders"]) == 0
    assert captured[0]["manual_live_orders"] is False
    assert captured[1]["manual_live_orders"] is True


def test_live_shortcut_enables_both_gates_only_for_app_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    key = "TOSS_ENABLE_MANUAL_LIVE_ORDERS"
    monkeypatch.setenv(key, "previous")

    class FakeApp:
        def __init__(self, symbol: str | None, credentials: object, **kwargs: object) -> None:
            captured.append({"symbol": symbol, "credentials": credentials, **kwargs})

        def run(self) -> int:
            assert os.environ[key] == "1"
            return 0

    monkeypatch.setattr(cli, "TossMarketApp", FakeApp)
    assert main(["live"]) == 0
    assert captured == [
        {
            "symbol": None,
            "credentials": cli.DEFAULT_CREDENTIALS_PATH,
            "settings_path": DEFAULT_SETTINGS_PATH,
            "manual_live_orders": True,
        }
    ]
    assert os.environ[key] == "previous"


def test_live_shortcut_keeps_optional_start_symbol_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str | None] = []

    class FakeApp:
        def __init__(self, symbol: str | None, *_args: object, **_kwargs: object) -> None:
            captured.append(symbol)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(cli, "TossMarketApp", FakeApp)
    assert main(["live", "aapl"]) == 0
    assert captured == ["AAPL"]


def test_live_shortcut_restores_missing_gate_when_app_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "TOSS_ENABLE_MANUAL_LIVE_ORDERS"
    monkeypatch.delenv(key, raising=False)

    class FailingApp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self) -> int:
            assert os.environ[key] == "1"
            raise RuntimeError("fixture failure")

    monkeypatch.setattr(cli, "TossMarketApp", FailingApp)
    with pytest.raises(RuntimeError, match="fixture failure"):
        main(["live"])
    assert key not in os.environ


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["watchlist", "add", "bad symbol!"], "심볼"),
        (["watchlist", "remove", "NVDA"], "없는 심볼"),
        (["alert", "add", "AAPL", "above", "0"], "임계값"),
        (["alert", "add", "AAPL", "above", "-1"], "임계값"),
        (["alert", "add", "AAPL", "rocket", "1"], None),
        (["alert", "add", "AAPL", "above", "abc"], "임계값"),
        (["alert", "remove", "A99"], "존재하지 않는 알림 ID"),
    ],
)
def test_mutating_errors_exit_nonzero(
    argv: list[str], needle: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    if needle is None:
        # argparse rejects unknown kinds with its own exit code 2 usage error.
        with pytest.raises(SystemExit) as caught:
            main(argv)
        assert caught.value.code == 2
        return
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert needle in captured.err


# --- v0.6 account command ----------------------------------------------------


def test_account_command_parses_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(["account", "AAPL"])
    assert (args.command, args.symbol, args.account_seq, args.json_output) == (
        "account",
        "AAPL",
        None,
        False,
    )
    args = parser.parse_args(["account", "005930", "--account-seq", "7", "--json"])
    assert (args.account_seq, args.json_output) == (7, True)


def test_account_command_rejects_non_positive_seq(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["account", "AAPL", "--account-seq", "0"]) == 2
    assert "양의 정수" in capsys.readouterr().err


def test_account_json_output_is_privacy_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fake_client(monkeypatch)
    code = main(["account", "aapl", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert FakeAccountClient.last_kwargs == {"symbol": "AAPL", "account_seq": None}
    # Boundary markers required by the contract.
    assert payload["scope"] == "account_read_only"
    assert payload["order_endpoints_called"] is False
    # Raw account number never appears; only the masked form does.
    assert "12345678901" not in json.dumps(payload)
    assert payload["account"]["masked_account_no"] == "*******8901"
    assert payload["buying_power"]["currency"] == "USD"
    assert payload["buying_power"]["cash_buying_power"] == "3500.5"


def test_account_json_honors_explicit_account_seq(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fake_client(monkeypatch)
    assert main(["account", "NVDA", "--account-seq", "3", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert FakeAccountClient.last_kwargs == {"symbol": "NVDA", "account_seq": 3}


def test_account_human_output_masks_account_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fake_client(monkeypatch)
    assert main(["account", "AAPL"]) == 0
    out = capsys.readouterr().out
    assert "*******8901" in out
    assert "account_read_only" in out


def test_account_payload_guard_rejects_unmasked_number() -> None:
    context = sample_context()
    broken = AccountContext(
        scope=context.scope,
        order_endpoints_called=context.order_endpoints_called,
        account=Account(
            account_seq=context.account.account_seq,
            account_type=context.account.account_type,
            masked_account_no="12345678901",
        ),
        symbol=context.symbol,
        holding=None,
        holding_quantity=Decimal("0"),
        buying_power=context.buying_power,
    )
    with pytest.raises(ValueError):
        cli.account_context_payload(broken)
