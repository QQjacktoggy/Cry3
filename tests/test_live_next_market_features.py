from decimal import Decimal

import pytest

from src.gridbot.strategy.live_next.exact_replay import ExactAggTrade
from src.gridbot.strategy.live_next.market_features import (
    FEATURE_ENGINE_VERSION,
    iter_causal_feature_frames,
)


def _trades(count=75):
    rows = []
    for index in range(count):
        if index < 65:
            price = Decimal("100")
        elif index == 65:
            price = Decimal("100.08")
        elif index == 66:
            price = Decimal("100.16")
        elif index == 67:
            price = Decimal("100.11")
        else:
            price = Decimal("100.12")
        rows.append(
            ExactAggTrade(
                transact_time_ms=1_700_000_000_100 + index * 1_000,
                agg_trade_id=1_000 + index,
                price=price,
                quantity=Decimal("1"),
                is_buyer_maker=index in {67},
            )
        )
    return tuple(rows)


def test_closed_bin_frames_are_causal_deterministic_and_outcome_blind():
    first = tuple(iter_causal_feature_frames(_trades()))
    second = tuple(iter_causal_feature_frames(_trades()))

    assert first == second
    assert first
    for frame in first:
        assert frame.market_data_max_event_ms < frame.decision_time_ms
        assert frame.snapshot.feature_version == FEATURE_ENGINE_VERSION
        assert frame.snapshot.market_data_max_event_ms == frame.market_data_max_event_ms
        assert "realized_pnl" not in frame.snapshot.values
        assert "mfe" not in frame.snapshot.values


def test_future_trades_do_not_change_an_already_closed_feature_frame():
    prefix = tuple(iter_causal_feature_frames(_trades(70)))
    full = tuple(iter_causal_feature_frames(_trades(75)))
    by_time = {frame.decision_time_ms: frame for frame in full}

    assert prefix
    for frame in prefix:
        assert frame == by_time[frame.decision_time_ms]


def test_impulse_retest_features_capture_move_retrace_and_flow():
    frames = tuple(iter_causal_feature_frames(_trades()))
    impulse = max(frames, key=lambda frame: abs(frame.snapshot.values["move_3s_bps"]))
    values = impulse.snapshot.values

    assert abs(values["move_3s_bps"]) >= 8.0
    assert 0.0 <= values["retrace_fraction"] <= 1.0
    assert 0.0 <= values["impulse_flow_ratio"] <= 1.0
    assert 0.0 <= values["trend_flow_ratio"] <= 1.0
    assert 0.0 <= values["range_inward_flow_ratio"] <= 1.0
    assert 0.0 <= values["shock_reversal_flow_ratio"] <= 1.0
    assert 0.0 <= values["trend_score"] <= 1.0
    assert 0.0 <= values["range_score"] <= 1.0
    assert 0.0 <= values["shock_score"] <= 1.0


def test_feature_engine_rejects_noncausal_order():
    rows = list(_trades(2))
    rows.reverse()
    with pytest.raises(ValueError, match="causal ordered"):
        tuple(iter_causal_feature_frames(rows))
