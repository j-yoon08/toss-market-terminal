from __future__ import annotations

from decimal import Decimal

import pytest

from toss_market_terminal.alerts import AlertEvaluator
from toss_market_terminal.render import MarketMetrics, MarketSignals
from toss_market_terminal.settings import AlertRule

EMPTY_METRICS = MarketMetrics(None, None, None, None, None, None, None, None, None)
EMPTY_SIGNALS = MarketSignals(None, None, None, None, None)


def rule(kind: str, threshold: str, *, alert_id: str = "A1", symbol: str = "AAPL") -> AlertRule:
    return AlertRule(alert_id, symbol, kind, Decimal(threshold))


@pytest.mark.parametrize(
    ("alert_rule", "first", "crossed", "rearmed"),
    [
        (rule("above", "100"), Decimal("100"), Decimal("101"), Decimal("99")),
        (rule("below", "100"), Decimal("100"), Decimal("99"), Decimal("101")),
    ],
)
def test_price_alerts_are_edge_triggered_and_rearm(
    alert_rule: AlertRule, first: Decimal, crossed: Decimal, rearmed: Decimal
) -> None:
    evaluator = AlertEvaluator()
    assert evaluator.evaluate(alert_rule, symbol="AAPL", current_price=first) is None
    event = evaluator.evaluate(alert_rule, symbol="AAPL", current_price=crossed)
    assert event is not None
    assert event.observed_value == crossed
    assert evaluator.evaluate(alert_rule, symbol="AAPL", current_price=crossed) is None
    assert evaluator.evaluate(alert_rule, symbol="AAPL", current_price=rearmed) is None
    assert evaluator.evaluate(alert_rule, symbol="AAPL", current_price=crossed) is not None


def test_first_true_observation_is_suppressed_to_prevent_startup_spam() -> None:
    evaluator = AlertEvaluator()
    alert_rule = rule("above", "100")
    assert evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("101")) is None
    assert evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("102")) is None


@pytest.mark.parametrize(
    ("kind", "threshold", "before", "after"),
    [
        ("change-above", "5", Decimal("5"), Decimal("5.01")),
        ("change-below", "5", Decimal("-5"), Decimal("-5.01")),
    ],
)
def test_change_percent_alerts_use_strict_signed_boundaries(
    kind: str, threshold: str, before: Decimal, after: Decimal
) -> None:
    evaluator = AlertEvaluator()
    alert_rule = rule(kind, threshold)
    first = MarketMetrics(None, None, before, None, None, None, None, None, None)
    crossed = MarketMetrics(None, None, after, None, None, None, None, None, None)
    assert (
        evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("100"), metrics=first)
        is None
    )
    assert (
        evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("100"), metrics=crossed)
        is not None
    )


def test_volume_spike_alert_requires_sufficient_signal_and_crossing() -> None:
    evaluator = AlertEvaluator()
    alert_rule = rule("volume-spike", "3")
    assert (
        evaluator.evaluate(
            alert_rule,
            symbol="AAPL",
            current_price=Decimal("100"),
            signals=EMPTY_SIGNALS,
        )
        is None
    )
    baseline = MarketSignals(None, None, None, Decimal("3"), None)
    crossed = MarketSignals(None, None, None, Decimal("3.1"), None)
    assert (
        evaluator.evaluate(
            alert_rule, symbol="AAPL", current_price=Decimal("100"), signals=baseline
        )
        is None
    )
    event = evaluator.evaluate(
        alert_rule,
        symbol="AAPL",
        current_price=Decimal("100"),
        signals=crossed,
        timestamp="2026-08-25T10:00:00Z",
    )
    assert event is not None
    assert "volume ratio > 3x" in event.message
    assert "2026-08-25T10:00:00Z" in event.message


def test_missing_observation_does_not_rearm_or_fire() -> None:
    evaluator = AlertEvaluator()
    alert_rule = rule("change-above", "5")
    below = MarketMetrics(None, None, Decimal("4"), None, None, None, None, None, None)
    above = MarketMetrics(None, None, Decimal("6"), None, None, None, None, None, None)
    assert (
        evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("100"), metrics=below)
        is None
    )
    assert (
        evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("100"), metrics=None)
        is None
    )
    assert (
        evaluator.evaluate(alert_rule, symbol="AAPL", current_price=Decimal("100"), metrics=above)
        is not None
    )


def test_rules_are_isolated_by_symbol_and_alert_id() -> None:
    evaluator = AlertEvaluator()
    aapl = rule("above", "100", alert_id="A1", symbol="AAPL")
    nvda = rule("above", "100", alert_id="A2", symbol="NVDA")
    assert evaluator.evaluate(aapl, symbol="NVDA", current_price=Decimal("99")) is None
    assert evaluator.evaluate(aapl, symbol="AAPL", current_price=Decimal("99")) is None
    assert evaluator.evaluate(nvda, symbol="NVDA", current_price=Decimal("99")) is None
    assert evaluator.evaluate(aapl, symbol="AAPL", current_price=Decimal("101")) is not None
    assert evaluator.evaluate(nvda, symbol="NVDA", current_price=Decimal("101")) is not None


def test_disabled_rule_never_establishes_or_fires() -> None:
    evaluator = AlertEvaluator()
    disabled = AlertRule("A1", "AAPL", "above", Decimal("100"), enabled=False)
    assert evaluator.evaluate(disabled, symbol="AAPL", current_price=Decimal("99")) is None
    assert evaluator.evaluate(disabled, symbol="AAPL", current_price=Decimal("101")) is None
