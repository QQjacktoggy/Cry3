from scripts.compare_trend350_tracks import (
    FIVE_MINUTES_MS,
    ONE_MINUTE_MS,
    TrackSignal,
    aggregate_completed_5m,
    aggregate_intrabar_5m,
    simulate_track,
)
from src.gridbot.strategy.long_pullback import Candle


def _minute(open_time_ms: int, price: float) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price + 0.5,
        volume=10,
        quote_volume=price * 10,
    )


def test_aggregate_completed_5m_excludes_partial_bucket():
    candles = [_minute(i * ONE_MINUTE_MS, 100 + i) for i in range(7)]

    completed = aggregate_completed_5m(candles, FIVE_MINUTES_MS + 2 * ONE_MINUTE_MS)

    assert len(completed) == 1
    assert completed[0].open_time_ms == 0
    assert completed[0].open == 100
    assert completed[0].high == 105
    assert completed[0].low == 99
    assert completed[0].close == 104.5


def test_aggregate_intrabar_5m_appends_partial_current_bucket():
    candles = [_minute(i * ONE_MINUTE_MS, 100 + i) for i in range(7)]

    snapshot = aggregate_intrabar_5m(candles, FIVE_MINUTES_MS + 2 * ONE_MINUTE_MS)

    assert len(snapshot) == 2
    partial = snapshot[-1]
    assert partial.open_time_ms == FIVE_MINUTES_MS
    assert partial.open == 105
    assert partial.high == 107
    assert partial.low == 104
    assert partial.close == 106.5


def test_simulate_track_honors_pending_busy_window():
    candles = [Candle(i * ONE_MINUTE_MS, 103, 104, 102, 103, 10) for i in range(60)]
    candles[10] = Candle(10 * ONE_MINUTE_MS, 101, 102, 99, 101, 10)
    first = _signal(0)
    second = _signal(ONE_MINUTE_MS)

    orders = simulate_track([first, second], candles, ttl_bars=8)

    assert len(orders) == 1
    assert orders[0].status == "filled"
    assert orders[0].exit_reason == "take_profit"


def _signal(signal_time_ms: int) -> TrackSignal:
    return TrackSignal(
        track="closed",
        signal_time_ms=signal_time_ms,
        candle_open_time_ms=signal_time_ms - FIVE_MINUTES_MS,
        direction="long",
        strategy="orb_long",
        regime="trend_up",
        risk_mode="normal",
        market_playbook="breakout",
        allocator_profile="trend",
        allocator_scale=1.0,
        score=90,
        confidence=90,
        market_price=101,
        planned_entry=100,
        order_entry=100,
        stop=95,
        take_profit=101.5,
        reward_pct=1.5,
        gap_bps=100,
        stale=False,
    )
