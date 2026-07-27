from scripts.freeze_live_next_research import EXIT_PROFILE_CONFIGS
from src.gridbot.strategy.live_next.replay import ExitProfile, ReplayCostModel


def _baseline_cost() -> ReplayCostModel:
    return ReplayCostModel(
        entry_fee_bps=2.0,
        exit_fee_bps=2.0,
        spread_slippage_bps=0.0,
        active_exit_fee_bps=5.0,
        active_exit_slippage_bps=1.0,
        funding_cost_usdc_per_fill=0.005,
    )


def test_v3_exits_disable_loss_making_no_mfe_t1_and_clear_cost_floor() -> None:
    cost = _baseline_cost()
    assert set(EXIT_PROFILE_CONFIGS) == {
        "net_tp24_sl8_hold30",
        "net_tp32_sl10_hold45",
    }
    for profile_id, parameters in EXIT_PROFILE_CONFIGS.items():
        profile = ExitProfile(
            profile_id=profile_id,
            take_profit_bps=parameters["take_profit_bps"],
            stop_loss_bps=parameters["stop_loss_bps"],
            t1_ms=parameters["t1_ms"],
            t1_min_mfe_bps=parameters["t1_min_mfe_bps"],
            t2_ms=parameters["t2_ms"],
        )
        assert profile.t1_min_mfe_bps == 0.0
        cost.assert_economic(profile)


def test_v3_full_notional_payoff_supports_20_fill_user_objective_at_70pct_wr() -> None:
    cost = _baseline_cost()
    notional = 50.0
    for parameters in EXIT_PROFILE_CONFIGS.values():
        tp = parameters["take_profit_bps"]
        sl = parameters["stop_loss_bps"]
        win_gross = notional * tp / 10_000.0
        win_cost = cost.all_in_cost_usdc(notional, notional, "TP")
        loss_gross = -notional * sl / 10_000.0
        loss_cost = cost.all_in_cost_usdc(notional, notional, "SL")
        twenty_fill_net = 14 * (win_gross - win_cost) + 6 * (
            loss_gross - loss_cost
        )

        assert twenty_fill_net > 0.75
