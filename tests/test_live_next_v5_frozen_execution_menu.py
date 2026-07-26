import json
from collections import Counter

from scripts.freeze_live_next_research import (
    COST_MODEL,
    EXECUTION_PROFILE_CONFIGS,
    build_contract,
)
from src.gridbot.strategy.live_next.execution_policy import (
    EntryExecutionMode,
    ExecutionPolicy,
)
from src.gridbot.strategy.live_next.replay import ReplayCostModel


def _cost_model() -> ReplayCostModel:
    return ReplayCostModel(
        entry_fee_bps=COST_MODEL["entry_fee_bps"],
        exit_fee_bps=COST_MODEL["tp_exit_fee_bps"],
        spread_slippage_bps=0.0,
        active_exit_fee_bps=COST_MODEL["active_exit_fee_bps"],
        active_exit_slippage_bps=COST_MODEL["active_adverse_slippage_bps"],
        funding_cost_usdc_per_fill=COST_MODEL["funding_cost_usdc_per_fill"],
        taker_entry_fee_bps=COST_MODEL["taker_entry_fee_bps"],
        taker_entry_slippage_bps=COST_MODEL["taker_entry_slippage_bps"],
    )


def test_v5_freezes_one_maker_one_taker_and_one_hybrid_profile():
    expected = {
        "maker_near_0bp": EntryExecutionMode.MAKER,
        "taker_confirm_100ms": EntryExecutionMode.TAKER_CONFIRM,
        "hybrid_maker0_500ms": EntryExecutionMode.HYBRID,
    }

    assert set(EXECUTION_PROFILE_CONFIGS) == set(expected)
    for profile_id, parameters in EXECUTION_PROFILE_CONFIGS.items():
        policy = ExecutionPolicy(profile_id=profile_id, **parameters)
        assert policy.mode is expected[profile_id]
        assert policy.max_reprices == 0

    contract = build_contract()
    assert len(contract["candidate_menu"]) == 24
    assert Counter(
        item["execution_profile"] for item in contract["candidate_menu"]
    ) == Counter({profile_id: 8 for profile_id in expected})
    for item in contract["candidate_menu"]:
        frozen = json.loads(item["parameters_json"])["execution"]
        assert frozen == EXECUTION_PROFILE_CONFIGS[item["execution_profile"]]


def test_wide_taker_exit_can_clear_canary_target_at_fifteen_wins_of_twenty():
    costs = _cost_model()
    notional = 50.0

    wide_win = notional * 32.0 / 10_000.0 - costs.all_in_cost_usdc(
        notional,
        notional * 1.0032,
        "TP",
        "TAKER",
    )
    wide_loss = -notional * 10.0 / 10_000.0 - costs.all_in_cost_usdc(
        notional,
        notional * 0.999,
        "SL",
        "TAKER",
    )
    balanced_win = notional * 24.0 / 10_000.0 - costs.all_in_cost_usdc(
        notional,
        notional * 1.0024,
        "TP",
        "TAKER",
    )
    balanced_loss = -notional * 8.0 / 10_000.0 - costs.all_in_cost_usdc(
        notional,
        notional * 0.9992,
        "SL",
        "TAKER",
    )

    assert 15 * wide_win + 5 * wide_loss > 0.75
    assert 15 * balanced_win + 5 * balanced_loss < 0.75
