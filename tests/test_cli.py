from __future__ import annotations

import pytest

from toss_market_terminal.cli import build_parser, main
from toss_market_terminal.settings import DEFAULT_SETTINGS_PATH


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
