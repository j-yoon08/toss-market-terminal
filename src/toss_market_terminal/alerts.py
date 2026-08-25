from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .render import MarketMetrics, MarketSignals
from .settings import AlertRule


@dataclass(frozen=True, slots=True)
class AlertEvent:
    rule: AlertRule
    observed_value: Decimal
    timestamp: str
    condition: str

    @property
    def message(self) -> str:
        return (
            f"{self.rule.id} · {self.rule.symbol} · {self.condition} · "
            f"observed={self.observed_value} · {self.timestamp}"
        )


def _observation(
    rule: AlertRule,
    *,
    current_price: Decimal | None,
    metrics: MarketMetrics | None,
    signals: MarketSignals | None,
) -> tuple[bool, Decimal, str] | None:
    if rule.kind == "above" and current_price is not None:
        return current_price > rule.threshold, current_price, f"price > {rule.threshold}"
    if rule.kind == "below" and current_price is not None:
        return current_price < rule.threshold, current_price, f"price < {rule.threshold}"
    if rule.kind == "change-above" and metrics is not None:
        value = metrics.change_percent
        if value is not None:
            return value > rule.threshold, value, f"change% > {rule.threshold}"
    if rule.kind == "change-below" and metrics is not None:
        value = metrics.change_percent
        if value is not None:
            boundary = -rule.threshold
            return value < boundary, value, f"change% < {boundary}"
    if rule.kind == "volume-spike" and signals is not None:
        value = signals.volume_spike_ratio
        if value is not None:
            return value > rule.threshold, value, f"volume ratio > {rule.threshold}x"
    return None


class AlertEvaluator:
    """Edge-triggered local evaluator keyed by stable alert ID.

    The first sufficient observation establishes state without firing. Missing
    observations do not re-arm an alert, which avoids duplicates during a brief
    REST or candle-data gap.
    """

    def __init__(self) -> None:
        self._conditions: dict[str, bool] = {}

    def evaluate(
        self,
        rule: AlertRule,
        *,
        symbol: str,
        current_price: Decimal | None,
        metrics: MarketMetrics | None = None,
        signals: MarketSignals | None = None,
        timestamp: str | None = None,
    ) -> AlertEvent | None:
        if not rule.enabled or rule.symbol != symbol:
            return None
        observation = _observation(
            rule,
            current_price=current_price,
            metrics=metrics,
            signals=signals,
        )
        if observation is None:
            return None
        condition, value, description = observation
        previous = self._conditions.get(rule.id)
        self._conditions[rule.id] = condition
        if previous is not False or not condition:
            return None
        observed_at = timestamp or datetime.now(UTC).isoformat(timespec="seconds")
        return AlertEvent(rule, value, observed_at, description)

    def evaluate_all(
        self,
        rules: Iterable[AlertRule],
        *,
        symbol: str,
        current_price: Decimal | None,
        metrics: MarketMetrics | None = None,
        signals: MarketSignals | None = None,
        timestamp: str | None = None,
    ) -> tuple[AlertEvent, ...]:
        events: list[AlertEvent] = []
        for rule in rules:
            event = self.evaluate(
                rule,
                symbol=symbol,
                current_price=current_price,
                metrics=metrics,
                signals=signals,
                timestamp=timestamp,
            )
            if event is not None:
                events.append(event)
        return tuple(events)
