from datetime import datetime, timezone

import pytest

from scripts.analyze_entry_fill_policy import compare_entry_fill_policies, parse_log_entry_events
from src.gridbot.testnet.fill_policy import (
    effective_entry_tolerance_bps,
    entry_limit_price,
    normalize_entry_fill_policy,
)


def test_fill_policy_normalizes_known_aliases():
    assert normalize_entry_fill_policy("trend350_strict") == "strict"
    assert normalize_entry_fill_policy("maker_tolerance") == "limit_tolerance"
    assert normalize_entry_fill_policy("unknown") == "strict"


def test_effective_tolerance_respects_policy_and_score():
    assert effective_entry_tolerance_bps("strict", 25, score=95) == 0
    assert effective_entry_tolerance_bps("limit_tolerance", 25, score=79, min_score=80) == 0
    assert effective_entry_tolerance_bps("limit_tolerance", 25, score=80, min_score=80) == 25


def test_entry_limit_price_caps_at_take_profit():
    assert entry_limit_price("long", 2100, 2070, 2100.5, 50) == pytest.approx(2100.4979)
    assert entry_limit_price("short", 2100, 2130, 2099.5, 50) == pytest.approx(2099.5021)


def test_log_policy_comparison_counts_tolerance_fill():
    lines = [
        "2026-05-24 01:21:13 [info     ] testnet_router_entry_limit_placed "
        "action=PLAN_LONG client_order_id=cry3en_1 entry=100 order_entry_price=100 "
        "order_id=1 score=84 stop=98 symbol=ETHUSDC take_profit=103 ttl_bars=8"
    ]
    events = parse_log_entry_events(
        lines,
        symbol="ETHUSDC",
        since=datetime(2026, 5, 24, 1, 0, tzinfo=timezone.utc),
    )

    candles = {"cry3en_1": [[0, "100", "101", "100.04", "100.5"]]}
    results = compare_entry_fill_policies(events, candles, tolerance_bps_values=[0, 5], min_reward_pct=0.12)

    assert results[0].filled == 0
    assert results[0].avg_miss_bps == pytest.approx(4.0)
    assert results[1].filled == 1
    assert results[1].avg_extra_bps == pytest.approx(5.0)
