from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from inspect import signature
from pathlib import Path

import pytest

from tests.helpers import patterned_candles, sample_snapshot
from toss_market_terminal import ai_direction as ai_module
from toss_market_terminal.ai_direction import (
    AiDirection,
    build_ai_direction_signal,
    direction_label_ko,
)


def signal_for_phase(phase: int):
    snapshot = replace(sample_snapshot(), candles=patterned_candles(final_phase=phase))
    return build_ai_direction_signal(snapshot, "5m")


def test_stale_data_always_fails_closed_before_sample_analysis() -> None:
    result = build_ai_direction_signal(sample_snapshot(), "1m", stale=True)
    assert result.direction is AiDirection.INSUFFICIENT
    assert result.data_quality == "stale"
    assert result.confidence_percent is None
    assert result.reasons == ("시세 신선도 저하",)
    assert result.advisory_only is True


def test_short_history_is_insufficient_instead_of_fabricating_direction() -> None:
    result = build_ai_direction_signal(sample_snapshot(), "1m")
    assert result.direction is AiDirection.INSUFFICIENT
    assert result.data_quality == "limited"
    assert "표본 부족" in result.reasons[0]
    assert result.confidence_percent is None


def test_non_finite_candle_fails_closed_without_echoing_raw_value() -> None:
    candles = list(patterned_candles())
    candles[0] = replace(candles[0], close_price=Decimal("NaN"))
    snapshot = replace(sample_snapshot(), candles=tuple(candles))
    result = build_ai_direction_signal(snapshot, "5m")
    assert result.direction is AiDirection.INSUFFICIENT
    assert result.data_quality == "invalid"
    assert result.reasons == ("캔들 데이터 검증 실패",)
    assert "NaN" not in " ".join(result.reasons)


def test_unsupported_mode_fails_closed() -> None:
    result = build_ai_direction_signal(sample_snapshot(), "weekly")
    assert result.direction is AiDirection.INSUFFICIENT
    assert result.data_quality == "invalid"


def test_walk_forward_score_below_floor_blocks_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_module,
        "_walk_forward_score",
        lambda _examples, _horizon: (Decimal("0.44"), 24),
    )
    result = signal_for_phase(4)
    assert result.direction is AiDirection.INSUFFICIENT
    assert result.validation_percent == 44
    assert result.reasons == ("walk-forward 성능 기준 미달",)


def test_low_validation_adjusted_confidence_downgrades_buy_to_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ai_module,
        "_walk_forward_score",
        lambda _examples, _horizon: (Decimal("0.45"), 24),
    )
    monkeypatch.setattr(ai_module, "MIN_ADJUSTED_CONFIDENCE", Decimal("0.50"))
    result = signal_for_phase(4)
    assert result.direction is AiDirection.HOLD
    assert result.data_quality == "limited"


def test_repeating_rising_pattern_produces_validated_buy_signal() -> None:
    result = signal_for_phase(4)
    assert result.direction is AiDirection.BUY
    assert result.data_quality == "validated"
    assert result.confidence_percent is not None
    assert result.validation_percent is not None
    assert result.validation_percent >= 45
    assert result.sample_size >= 48
    assert result.neighbor_count >= 7
    assert result.horizon_label == "약 5분"
    assert any("유사 패턴" in reason for reason in result.reasons)
    assert "평균 아래" in result.invalidation
    assert result.as_of is not None


def test_repeating_falling_pattern_produces_validated_sell_signal() -> None:
    result = signal_for_phase(14)
    assert result.direction is AiDirection.SELL
    assert result.data_quality == "validated"
    assert result.confidence_percent is not None
    assert result.validation_percent is not None
    assert "평균 위" in result.invalidation


def test_direction_labels_are_bounded_korean_ui_terms() -> None:
    assert direction_label_ko(AiDirection.BUY) == "매수"
    assert direction_label_ko(AiDirection.HOLD) == "관망"
    assert direction_label_ko(AiDirection.SELL) == "매도"
    assert direction_label_ko(AiDirection.INSUFFICIENT) == "판단 불가"


def test_public_builder_accepts_no_account_order_or_transport_inputs() -> None:
    parameters = signature(build_ai_direction_signal).parameters
    assert tuple(parameters) == ("snapshot", "mode", "stale")
    assert parameters["stale"].kind.name == "KEYWORD_ONLY"


def test_ai_module_has_no_account_network_or_order_imports() -> None:
    source_path = Path(ai_module.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
    assert not imported_modules.intersection(
        {
            "client",
            "config",
            "live_order",
            "order_preview",
            "order_transport",
            "portfolio",
        }
    )


@pytest.mark.parametrize("mode", ["1m", "5m", "15m", "1h", "1d"])
def test_every_supported_mode_keeps_output_advisory_only(mode: str) -> None:
    result = build_ai_direction_signal(sample_snapshot(), mode)
    assert result.advisory_only is True
    assert result.policy_version == "display-only-v1"
