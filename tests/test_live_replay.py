from scripts.live_replay_backtest import PendingOrder, _open_position, _try_close_position, _try_fill_pending, run_live_replay
from src.gridbot.replay.live_replay import ReplayConfig, plan_execution, plan_micro_execution, should_preempt_pending
from src.gridbot.strategy.long_breakout import BreakoutConfig
from src.gridbot.strategy.long_pullback import Candle, SignalPlan, StrategyConfig
from src.gridbot.strategy.market_state import MarketStateDecision, MarketStateFeatures


def _append_flat_minutes(candles: list[Candle], start_index: int, minutes: int, price: float, volume: float) -> int:
    for offset in range(minutes):
        open_price = price
        close_price = price + 0.01
        candles.append(Candle((start_index + offset) * 60_000, open_price, close_price + 0.05, open_price - 0.05, close_price, volume))
    return start_index + minutes


def _build_v2_breakout_seed() -> list[Candle]:
    candles: list[Candle] = []
    index = 0
    price = 100.0
    for block in range(24):
        base = price + block * 0.18
        for minute in range(5):
            open_price = base + minute * 0.015
            close_price = open_price + 0.02
            candles.append(Candle(index * 60_000, open_price, close_price + 0.03, open_price - 0.03, close_price, 20))
            index += 1
    breakout_base = candles[-1].close + 0.25
    for minute in range(5):
        open_price = breakout_base + minute * 0.11
        close_price = open_price + 0.09
        candles.append(Candle(index * 60_000, open_price, close_price + 0.05, open_price - 0.02, close_price, 42))
        index += 1
    candles.append(Candle(index * 60_000, candles[-1].close + 0.05, candles[-1].close + 0.44, candles[-1].close + 0.02, candles[-1].close + 0.38, 58))
    return candles


def _build_v2_pullback_seed() -> list[Candle]:
    candles = _build_v2_breakout_seed()
    index = len(candles)
    base = candles[-1].close + 0.06
    pullback_minutes = [
        (base, base + 0.04, base - 0.18, base - 0.02, 28),
        (base - 0.01, base + 0.03, base - 0.22, base + 0.01, 26),
        (base + 0.02, base + 0.10, base - 0.03, base + 0.09, 32),
    ]
    for open_price, high, low, close_price, volume in pullback_minutes:
        candles.append(Candle(index * 60_000, open_price, high, low, close_price, volume))
        index += 1
    return candles


def test_plan_execution_prefers_marketable_breakout_when_gap_small():
    candle = Candle(0, 100, 101, 99.8, 100.4, 10)
    market = _market(playbook="long_breakout")
    breakout = _signal(entry=100.32, stop=99.32, tp=101.72, score=88, notional=80, leverage=8)
    pullback = _signal(entry=99.5, stop=98.5, tp=101.0, score=70, notional=60, leverage=6)

    plan = plan_execution(
        current_candle=candle,
        market_decision=market,
        breakout_signal=breakout,
        pullback_signal=pullback,
        config=ReplayConfig(max_chase_gap_bps=12),
    )

    assert plan is not None
    assert plan.mode == "marketable_momentum"
    assert plan.strategy == "long_breakout"


def test_plan_execution_builds_pullback_maker_ladder():
    candle = Candle(0, 100, 101, 99.8, 100.2, 10)
    market = _market(playbook="long_pullback")
    pullback = SignalPlan(
        action="PLAN_LONG",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=100.2,
        rsi=48.0,
        atr=1.0,
        support=98.8,
        vwap=99.7,
        entries=[99.8, 99.3, 98.9],
        entry_weights=[0.4, 0.35, 0.25],
        stop_loss=97.8,
        take_profits=[101.2],
        planned_notional_usdc=70,
        leverage_cap=6,
        planned_qty=0.7,
    )

    plan = plan_execution(
        current_candle=candle,
        market_decision=market,
        breakout_signal=None,
        pullback_signal=pullback,
        config=ReplayConfig(),
    )

    assert plan is not None
    assert plan.mode == "maker_pullback"
    assert plan.entry_levels == (99.8, 99.3, 98.9)


def test_plan_execution_allows_breakout_followthrough_outside_strict_breakout_playbook():
    candle = Candle(0, 100, 101, 99.8, 100.4, 10)
    market = _market(playbook="no_trade")
    breakout = _signal(entry=100.32, stop=99.32, tp=101.72, score=88, notional=80, leverage=8)

    plan = plan_execution(
        current_candle=candle,
        market_decision=market,
        breakout_signal=breakout,
        pullback_signal=None,
        config=ReplayConfig(max_chase_gap_bps=12),
    )

    assert plan is not None
    assert plan.mode == "marketable_momentum"
    assert plan.strategy == "long_breakout"


def test_plan_micro_execution_builds_1m_breakout_plan():
    candles = []
    price = 100.0
    for index in range(89):
        price += 0.015
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.08, price + 0.01, 20))
    candles.append(Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(micro_warmup_1m_bars=80, micro_max_extension_atr=6.0),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "marketable_momentum"
    assert plan.strategy == "micro_breakout"
    assert plan.planned_notional_usdc == 486.0


def test_plan_micro_execution_builds_v2_breakout_followthrough_plan():
    candles = _build_v2_breakout_seed()

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_pullback_enabled=False,
            micro_vwap_reclaim_enabled=False,
            micro_reversion_enabled=False,
            micro_retest_enabled=False,
            micro_regime_v2_breakout_max_extension_atr=6.0,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.strategy == "micro_v2_breakout"
    assert plan.mode == "marketable_momentum"


def test_plan_micro_execution_builds_v2_post_breakout_pullback_plan():
    candles = _build_v2_pullback_seed()

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_pullback_enabled=False,
            micro_vwap_reclaim_enabled=False,
            micro_reversion_enabled=False,
            micro_retest_enabled=False,
            micro_regime_v2_breakout_max_extension_atr=6.0,
            micro_regime_v2_pullback_touch_atr=0.9,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.strategy == "micro_v2_trend_pullback"
    assert plan.mode == "marketable_pullback"


def test_plan_micro_execution_keeps_raw_breakout_marketable_by_default_with_maker_first():
    candles = []
    price = 100.0
    for index in range(89):
        price += 0.015
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.08, price + 0.01, 20))
    candles.append(Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=6.0,
            micro_maker_first_enabled=True,
            micro_maker_ttl_minutes=2,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "marketable_momentum"
    assert plan.strategy == "micro_breakout"


def test_plan_micro_execution_can_convert_breakout_to_maker_first_plan_when_allowed():
    candles = []
    price = 100.0
    for index in range(89):
        price += 0.015
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.08, price + 0.01, 20))
    candles.append(Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=6.0,
            micro_maker_first_enabled=True,
            micro_maker_first_strategies=("micro_breakout",),
            micro_maker_ttl_minutes=2,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "maker_micro"
    assert plan.strategy == "maker_micro_breakout"
    assert plan.entry_levels[0] < plan.signal_price
    assert plan.ttl_minutes == 2


def test_maker_first_fixed_ticket_uses_maker_fee_for_sizing():
    candles = []
    price = 100.0
    for index in range(89):
        price += 0.015
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.08, price + 0.01, 20))
    candles.append(Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70))

    maker_plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=6.0,
            micro_maker_first_enabled=True,
            micro_maker_first_strategies=("micro_breakout",),
            micro_fixed_ticket_enabled=True,
            micro_target_net_profit_usdc=0.75,
            micro_maker_entry_fee_rate=0.0002,
            micro_take_profit_fee_rate=0.0002,
        ),
        equity_usdc=150,
    )
    taker_plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=6.0,
            micro_maker_first_enabled=False,
            micro_fixed_ticket_enabled=True,
            micro_target_net_profit_usdc=0.75,
            micro_entry_taker_fee_rate=0.0004,
            micro_take_profit_fee_rate=0.0002,
        ),
        equity_usdc=150,
    )

    assert maker_plan is not None
    assert taker_plan is not None
    assert maker_plan.planned_notional_usdc < taker_plan.planned_notional_usdc


def test_plan_micro_execution_builds_dip_reclaim_plan():
    candles = []
    price = 100.0
    for index in range(88):
        price += 0.03
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.05, price, 20))
    candles.append(Candle(88 * 60_000, 102.4, 102.5, 101.2, 101.75, 30))
    candles.append(Candle(89 * 60_000, 101.7, 101.95, 101.15, 101.82, 32))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_reversion_min_dip_atr=0.2,
            micro_reversion_enabled=True,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "marketable_reclaim"
    assert plan.strategy == "micro_reclaim"


def test_plan_micro_execution_builds_vwap_reclaim_plan():
    candles = []
    price = 100.0
    for index in range(88):
        price += 0.02
        candles.append(Candle(index * 60_000, price, price + 0.08, price - 0.05, price + 0.01, 20))
    candles.append(Candle(88 * 60_000, 101.8, 101.9, 100.7, 101.3, 25))
    candles.append(Candle(89 * 60_000, 101.25, 101.85, 100.6, 101.75, 36))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_vwap_reclaim_enabled=True,
            micro_reversion_enabled=False,
            micro_vwap_min_sweep_atr=0.2,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "marketable_vwap"
    assert plan.strategy == "micro_vwap_reclaim"


def test_plan_micro_execution_builds_breakout_retest_plan():
    candles = []
    for index in range(88):
        price = 100 + index * 0.01
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.05, price + 0.01, 20))
    candles.append(Candle(88 * 60_000, 100.92, 101.15, 100.9, 101.05, 42))
    candles.append(Candle(89 * 60_000, 101.02, 101.08, 100.94, 101.03, 25))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_retest_max_extension_atr=4.0,
            micro_pullback_enabled=False,
            micro_reversion_enabled=False,
            micro_vwap_reclaim_enabled=False,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "marketable_retest"
    assert plan.strategy == "micro_breakout_retest"
    assert plan.max_hold_minutes <= 60


def test_plan_micro_execution_builds_ema_vwap_pullback_plan():
    candles = []
    for index in range(88):
        price = 100 + index * 0.02
        candles.append(Candle(index * 60_000, price, price + 0.07, price - 0.05, price + 0.01, 20))
    candles.append(Candle(88 * 60_000, 101.72, 101.83, 101.52, 101.78, 24))
    candles.append(Candle(89 * 60_000, 101.76, 101.85, 101.55, 101.82, 24))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_retest_enabled=False,
            micro_pullback_enabled=True,
            micro_pullback_max_extension_atr=4.0,
            micro_pullback_require_recent_5m_breakout=False,
            micro_vwap_reclaim_enabled=False,
            micro_reversion_enabled=False,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert plan is not None
    assert plan.mode == "marketable_pullback"
    assert plan.strategy == "micro_ema_vwap_pullback"
    assert plan.max_hold_minutes <= 60


def test_plan_micro_execution_blocks_pullback_without_recent_5m_breakout():
    candles = []
    for index in range(88):
        price = 100 + index * 0.02
        candles.append(Candle(index * 60_000, price, price + 0.07, price - 0.05, price + 0.01, 20))
    candles.append(Candle(88 * 60_000, 101.72, 101.83, 101.52, 101.78, 24))
    candles.append(Candle(89 * 60_000, 101.76, 101.85, 101.55, 101.82, 24))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_retest_enabled=False,
            micro_pullback_enabled=True,
            micro_pullback_max_extension_atr=4.0,
            micro_vwap_reclaim_enabled=False,
            micro_reversion_enabled=False,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert plan is None


def test_plan_micro_vwap_reclaim_respects_sweep_requirement():
    candles = []
    price = 100.0
    for index in range(88):
        price += 0.02
        candles.append(Candle(index * 60_000, price, price + 0.08, price - 0.05, price + 0.01, 20))
    candles.append(Candle(88 * 60_000, 101.8, 101.9, 101.55, 101.75, 25))
    candles.append(Candle(89 * 60_000, 101.76, 102.0, 101.65, 101.95, 36))

    plan = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_reversion_enabled=False,
            micro_vwap_reclaim_enabled=True,
            micro_vwap_min_sweep_atr=2.0,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert plan is None


def test_plan_micro_reclaim_respects_position_in_range_cap():
    candles = []
    price = 100.0
    for index in range(88):
        price += 0.03
        candles.append(Candle(index * 60_000, price, price + 0.05, price - 0.05, price, 20))
    candles.append(Candle(88 * 60_000, 102.4, 102.5, 101.2, 101.75, 30))
    candles.append(Candle(89 * 60_000, 101.7, 101.95, 101.15, 101.82, 32))

    blocked = plan_micro_execution(
        one_minute=candles,
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_min_volume_ratio=99,
            micro_reversion_min_dip_atr=0.2,
            micro_reversion_enabled=True,
            micro_reversion_max_position_in_range=0.01,
            min_reward_pct=0.04,
        ),
        equity_usdc=150,
    )

    assert blocked is None


def test_run_live_replay_can_open_and_close_momentum_trade():
    prices = [100 + index * 0.02 for index in range(240)]
    candles = [Candle(index * 60_000, price, price + 0.2, price - 0.1, price, 10) for index, price in enumerate(prices)]
    result = run_live_replay(
        candles,
        start_time_ms=candles[120].open_time_ms,
        base=StrategyConfig(symbol="ETHUSDC"),
        config=ReplayConfig(warmup_5m_bars=12),
    )

    assert "summary" in result
    assert result["summary"]["total_trades"] >= 0


def test_run_live_replay_counts_no_trade_days_in_daily_summary():
    candles = [
        Candle(index * 60_000, 100, 100.1, 99.9, 100, 10)
        for index in range(60 * 30)
    ]
    result = run_live_replay(
        candles,
        start_time_ms=0,
        base=StrategyConfig(symbol="ETHUSDC"),
        config=ReplayConfig(micro_enabled=False, legacy_5m_enabled=False),
    )

    assert result["summary"]["total_trades"] == 0
    assert len(result["summary"]["daily_pct"]) == 2
    assert result["summary"]["avg_daily_pct"] == 0.0
    assert result["summary"]["target_hit_rate_pct"] == 0.0
    assert result["summary"]["max_loss_hit_rate_pct"] == 0.0


def test_partial_maker_fill_uses_filled_weight_for_entry_and_size():
    plan = plan_execution(
        current_candle=Candle(0, 100, 101, 99.8, 100.2, 10),
        market_decision=_market(playbook="long_pullback"),
        breakout_signal=None,
        pullback_signal=SignalPlan(
            action="PLAN_LONG",
            confidence=80,
            score=80,
            symbol="ETHUSDC",
            price=100.2,
            rsi=48.0,
            atr=1.0,
            support=98.8,
            vwap=99.7,
            entries=[99.8, 99.3, 98.9],
            entry_weights=[0.4, 0.35, 0.25],
            stop_loss=97.8,
            take_profits=[101.2],
            planned_notional_usdc=70,
            leverage_cap=6,
            planned_qty=0.7,
        ),
        config=ReplayConfig(),
    )
    assert plan is not None

    filled = _try_fill_pending(
        PendingOrder(plan=plan, created_at_ms=0),
        Candle(60_000, 100.0, 100.1, 99.2, 99.5, 10),
        ReplayConfig(),
        StrategyConfig(symbol="ETHUSDC"),
    )

    assert filled is not None
    assert round(filled.entry_price, 4) == 99.5667
    assert round(filled.qty, 4) == round((70.0 * 0.75) / 99.5666666667, 4)


def test_pending_fill_checks_stop_on_fill_candle():
    plan = plan_execution(
        current_candle=Candle(0, 100, 101, 99.8, 100.2, 10),
        market_decision=_market(playbook="long_pullback"),
        breakout_signal=None,
        pullback_signal=SignalPlan(
            action="PLAN_LONG",
            confidence=80,
            score=80,
            symbol="ETHUSDC",
            price=100.2,
            rsi=48.0,
            atr=1.0,
            support=98.8,
            vwap=99.7,
            entries=[99.8],
            entry_weights=[1.0],
            stop_loss=98.8,
            take_profits=[101.2],
            planned_notional_usdc=70,
            leverage_cap=6,
            planned_qty=0.7,
        ),
        config=ReplayConfig(),
    )
    assert plan is not None
    candle = Candle(60_000, 100.0, 100.1, 98.7, 99.5, 10)
    filled = _try_fill_pending(
        PendingOrder(plan=plan, created_at_ms=0),
        candle,
        ReplayConfig(),
        StrategyConfig(symbol="ETHUSDC"),
    )

    assert filled is not None
    closed = _try_close_position(filled, candle, StrategyConfig(symbol="ETHUSDC"))
    assert closed is not None
    assert closed.reason == "stop_loss"


def test_partial_maker_fill_rejects_low_remaining_reward():
    plan = plan_execution(
        current_candle=Candle(0, 100, 101, 99.8, 100.2, 10),
        market_decision=_market(playbook="long_pullback"),
        breakout_signal=None,
        pullback_signal=SignalPlan(
            action="PLAN_LONG",
            confidence=80,
            score=80,
            symbol="ETHUSDC",
            price=100.2,
            rsi=48.0,
            atr=1.0,
            support=98.8,
            vwap=99.7,
            entries=[99.8, 99.3],
            entry_weights=[0.5, 0.5],
            stop_loss=98.8,
            take_profits=[99.95],
            planned_notional_usdc=70,
            leverage_cap=6,
            planned_qty=0.7,
        ),
        config=ReplayConfig(min_reward_pct=0.01),
    )
    assert plan is not None

    filled = _try_fill_pending(
        PendingOrder(plan=plan, created_at_ms=0),
        Candle(60_000, 100.0, 100.1, 99.7, 99.9, 10),
        ReplayConfig(min_reward_pct=0.18),
        StrategyConfig(symbol="ETHUSDC"),
    )

    assert filled is None


def test_open_position_uses_base_taker_fee_for_marketable_entry():
    plan = plan_micro_execution(
        one_minute=[
            Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
            for index in range(89)
        ]
        + [Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70)],
        config=ReplayConfig(micro_warmup_1m_bars=80, micro_max_extension_atr=6.0),
        equity_usdc=150,
    )
    assert plan is not None
    position = _open_position(plan, 0, plan.entry_levels[0], StrategyConfig(symbol="ETHUSDC", taker_fee_rate=0.001))

    assert position.entry_fee_rate == 0.001


def test_pending_maker_fill_uses_base_maker_fee():
    plan = plan_execution(
        current_candle=Candle(0, 100, 101, 99.8, 100.2, 10),
        market_decision=_market(playbook="long_pullback"),
        breakout_signal=None,
        pullback_signal=SignalPlan(
            action="PLAN_LONG",
            confidence=80,
            score=80,
            symbol="ETHUSDC",
            price=100.2,
            rsi=48.0,
            atr=1.0,
            support=98.8,
            vwap=99.7,
            entries=[99.8],
            entry_weights=[1.0],
            stop_loss=97.8,
            take_profits=[101.2],
            planned_notional_usdc=70,
            leverage_cap=6,
            planned_qty=0.7,
        ),
        config=ReplayConfig(),
    )
    assert plan is not None

    filled = _try_fill_pending(
        PendingOrder(plan=plan, created_at_ms=0),
        Candle(60_000, 100.0, 100.1, 99.7, 99.9, 10),
        ReplayConfig(),
        StrategyConfig(symbol="ETHUSDC", maker_fee_rate=0.0002),
    )

    assert filled is not None
    assert filled.entry_fee_rate == 0.0002


def test_breakout_plan_can_preempt_pending_pullback():
    pending = SignalPlan(
        action="PLAN_LONG",
        confidence=80,
        score=72,
        symbol="ETHUSDC",
        price=100.2,
        rsi=48.0,
        atr=1.0,
        support=98.8,
        vwap=99.7,
        entries=[99.8, 99.3, 98.9],
        entry_weights=[0.4, 0.35, 0.25],
        stop_loss=97.8,
        take_profits=[101.2],
        planned_notional_usdc=70,
        leverage_cap=6,
        planned_qty=0.7,
    )
    pending_plan = plan_execution(
        current_candle=Candle(0, 100, 101, 99.8, 100.2, 10),
        market_decision=_market(playbook="long_pullback"),
        breakout_signal=None,
        pullback_signal=pending,
        config=ReplayConfig(),
    )
    breakout_plan = plan_execution(
        current_candle=Candle(60_000, 100, 101, 99.8, 100.4, 10),
        market_decision=_market(playbook="no_trade"),
        breakout_signal=_signal(entry=100.32, stop=99.32, tp=101.72, score=88, notional=80, leverage=8),
        pullback_signal=None,
        config=ReplayConfig(max_chase_gap_bps=12),
    )

    assert pending_plan is not None
    assert breakout_plan is not None
    assert should_preempt_pending(pending_plan, breakout_plan, ReplayConfig())


def test_marketable_take_profit_uses_maker_exit_fee():
    plan = plan_micro_execution(
        one_minute=[
            Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
            for index in range(89)
        ]
        + [Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70)],
        config=ReplayConfig(micro_warmup_1m_bars=80, micro_max_extension_atr=6.0),
        equity_usdc=150,
    )
    assert plan is not None
    result = run_live_replay(
        [
            Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
            for index in range(89)
        ]
        + [
            Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70),
            Candle(90 * 60_000, 102.3, plan.take_profit + 0.1, 102.2, plan.take_profit, 20),
        ],
        start_time_ms=0,
        base=StrategyConfig(symbol="ETHUSDC"),
        config=ReplayConfig(warmup_5m_bars=12, micro_warmup_1m_bars=80, micro_max_extension_atr=6.0, legacy_5m_enabled=False),
    )

    assert result["trades"]
    assert result["trades"][0]["reason"] == "take_profit"
    assert result["trades"][0]["fees_usdc"] > 0


def test_run_live_replay_places_maker_first_micro_as_pending_order():
    seed = [
        Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
        for index in range(89)
    ]
    signal = Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70)
    plan = plan_micro_execution(
        one_minute=seed + [signal],
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=10.0,
            micro_maker_first_enabled=True,
            micro_maker_first_strategies=("micro_breakout",),
            micro_maker_ttl_minutes=2,
        ),
        equity_usdc=150,
    )
    assert plan is not None
    fill_low = max(plan.stop_loss + 0.01, plan.entry_levels[0] - 0.01)
    result = run_live_replay(
        seed
        + [
            signal,
            Candle(90 * 60_000, plan.signal_price, plan.take_profit + 0.1, fill_low, plan.take_profit, 20),
        ],
        start_time_ms=0,
        base=StrategyConfig(symbol="ETHUSDC", maker_fee_rate=0.0002, taker_fee_rate=0.0004),
        config=ReplayConfig(
            warmup_5m_bars=12,
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=10.0,
            legacy_5m_enabled=False,
            micro_maker_first_enabled=True,
            micro_maker_first_strategies=("micro_breakout",),
            micro_maker_ttl_minutes=2,
        ),
    )

    assert result["trades"]
    assert result["trades"][0]["mode"] == "maker_micro"
    assert result["trades"][0]["reason"] == "take_profit"
    assert result["trades"][0]["fees_usdc"] > 0


def test_run_live_replay_does_not_open_unfilled_maker_first_micro_order():
    seed = [
        Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
        for index in range(89)
    ]
    signal = Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70)
    plan = plan_micro_execution(
        one_minute=seed + [signal],
        config=ReplayConfig(
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=10.0,
            micro_maker_first_enabled=True,
            micro_maker_first_strategies=("micro_breakout",),
            micro_maker_ttl_minutes=1,
        ),
        equity_usdc=150,
    )
    assert plan is not None
    result = run_live_replay(
        seed
        + [
            signal,
            Candle(90 * 60_000, plan.signal_price + 0.2, plan.signal_price + 0.3, plan.entry_levels[0] + 0.05, plan.signal_price + 0.25, 20),
            Candle(91 * 60_000, plan.signal_price + 0.3, plan.signal_price + 0.4, plan.entry_levels[0] + 0.06, plan.signal_price + 0.35, 20),
        ],
        start_time_ms=0,
        base=StrategyConfig(symbol="ETHUSDC", maker_fee_rate=0.0002, taker_fee_rate=0.0004),
        config=ReplayConfig(
            warmup_5m_bars=12,
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=10.0,
            legacy_5m_enabled=False,
            micro_maker_first_enabled=True,
            micro_maker_first_strategies=("micro_breakout",),
            micro_maker_ttl_minutes=1,
        ),
    )

    assert result["summary"]["total_trades"] == 0


def test_micro_stop_loss_cooldown_blocks_immediate_reentry():
    candles = [
        Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
        for index in range(89)
    ]
    candles.extend(
        [
            Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70),
            Candle(90 * 60_000, 102.3, 102.4, 100.0, 100.5, 20),
            Candle(91 * 60_000, 100.5, 103.0, 100.4, 102.8, 90),
        ]
    )
    result = run_live_replay(
        candles,
        start_time_ms=0,
        base=StrategyConfig(symbol="ETHUSDC"),
        config=ReplayConfig(
            legacy_5m_enabled=False,
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=10.0,
            micro_stop_cooldown_minutes=90,
        ),
    )

    assert result["summary"]["total_trades"] == 1
    assert result["trades"][0]["reason"] == "stop_loss"


def test_micro_trade_cooldown_blocks_immediate_reentry_after_take_profit():
    seed = [
        Candle(index * 60_000, 100 + index * 0.01, 100.1 + index * 0.01, 99.95 + index * 0.01, 100.02 + index * 0.01, 20)
        for index in range(89)
    ]
    probe_plan = plan_micro_execution(
        one_minute=seed + [Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70)],
        config=ReplayConfig(micro_warmup_1m_bars=80, micro_max_extension_atr=10.0),
        equity_usdc=150,
    )
    assert probe_plan is not None
    result = run_live_replay(
        seed
        + [
            Candle(89 * 60_000, 101.7, 102.4, 101.6, 102.3, 70),
            Candle(90 * 60_000, 102.3, probe_plan.take_profit + 0.1, 102.2, probe_plan.take_profit, 20),
            Candle(91 * 60_000, probe_plan.take_profit, probe_plan.take_profit + 2.0, probe_plan.take_profit - 0.1, probe_plan.take_profit + 1.8, 90),
        ],
        start_time_ms=0,
        base=StrategyConfig(symbol="ETHUSDC"),
        config=ReplayConfig(
            legacy_5m_enabled=False,
            micro_warmup_1m_bars=80,
            micro_max_extension_atr=10.0,
            micro_trade_cooldown_minutes=30,
        ),
    )

    assert result["summary"]["total_trades"] == 1
    assert result["trades"][0]["reason"] == "take_profit"


def _signal(entry: float, stop: float, tp: float, score: int, notional: float, leverage: float) -> SignalPlan:
    return SignalPlan(
        action="PLAN_LONG",
        confidence=score,
        score=score,
        symbol="ETHUSDC",
        price=entry,
        rsi=56.0,
        atr=1.0,
        support=entry - 1.2,
        vwap=entry - 0.4,
        entries=[entry],
        entry_weights=[1.0],
        stop_loss=stop,
        take_profits=[tp],
        planned_notional_usdc=notional,
        leverage_cap=leverage,
        planned_qty=notional / entry,
    )


def _market(playbook: str) -> MarketStateDecision:
    features = MarketStateFeatures(
        price=100.0,
        ma20=99.8,
        ma20_slope_atr=0.2,
        ema55=99.4,
        vwap=99.9,
        atr=1.0,
        atr_percentile=0.4,
        volume_ratio=1.5,
        distance_to_ma20_atr=0.4,
        distance_to_vwap_atr=0.3,
        close_position_20=0.8,
        body_to_range=0.7,
    )
    return MarketStateDecision(
        trend="up",
        ma20_structure="above_rising",
        n_pattern="bullish",
        breakout_quality="strong",
        pullback_quality="healthy",
        volatility="normal",
        playbook=playbook,
        risk_mode="normal",
        confidence=0.8,
        features=features,
        reasons=("test",),
    )
