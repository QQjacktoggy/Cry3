"""Win-Rate Optimized Multi-Strategy Portfolio for Live Trading."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from src.gridbot.strategy.long_pullback import Candle, SignalPlan
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")

# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS FOR LIVE EXECUTOR INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LiveRouterDecision:
    signal: SignalPlan
    strategy: str
    regime: str = "unknown"
    risk_mode: str = "unknown"
    market_playbook: str = "unknown"
    allocator_state: str = "unknown"
    allocator_profile: str = "unknown"
    allocator_scale: float = 1.0
    max_holding_bars: int = 48

# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATOR CALCULATORS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_ema(prices: list[float], period: int) -> list[float | None]:
    ema = [None] * len(prices)
    if len(prices) < period:
        return ema
    alpha = 2 / (period + 1)
    current = sum(prices[:period]) / period
    ema[period - 1] = current
    for i in range(period, len(prices)):
        current = alpha * prices[i] + (1 - alpha) * current
        ema[i] = current
    return ema

def calculate_atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    atr = [None] * len(candles)
    if len(candles) <= period:
        return atr
    tr_list = [0.0] * len(candles)
    for i in range(1, len(candles)):
        c = candles[i]
        prev_c = candles[i-1]
        tr_list[i] = max(c.high - c.low, abs(c.high - prev_c.close), abs(c.low - prev_c.close))
    
    curr_atr = sum(tr_list[1:period+1]) / period
    atr[period] = curr_atr
    for i in range(period + 1, len(candles)):
        curr_atr = (curr_atr * (period - 1) + tr_list[i]) / period
        atr[i] = curr_atr
    return atr

def calculate_bollinger_bands(prices: list[float], period: int = 20, multiplier: float = 2.0) -> tuple[list[float | None], list[float | None], list[float | None]]:
    upper = [None] * len(prices)
    mid = [None] * len(prices)
    lower = [None] * len(prices)
    if len(prices) < period:
        return upper, mid, lower
    
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        std = math.sqrt(variance)
        mid[i] = m
        upper[i] = m + multiplier * std
        lower[i] = m - multiplier * std
    return upper, mid, lower

def calculate_rsi(prices: list[float], period: int = 14) -> list[float | None]:
    rsi = [None] * len(prices)
    if len(prices) <= period:
        return rsi
    
    gains = [0.0] * len(prices)
    losses = [0.0] * len(prices)
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)
        
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    rsi[period] = 100.0 if avg_loss == 0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    
    for i in range(period + 1, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = 100.0 if avg_loss == 0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return rsi

def calculate_macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float | None], list[float | None], list[float | None]]:
    macd_line = [None] * len(prices)
    signal_line = [None] * len(prices)
    macd_hist = [None] * len(prices)
    
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    
    for i in range(len(prices)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
            
    # Calculate EMA of macd_line for signal line
    macd_valid = [x for x in macd_line if x is not None]
    if len(macd_valid) >= signal:
        alpha = 2 / (signal + 1)
        curr_sig = sum(macd_valid[:signal]) / signal
        start_idx = macd_line.index(macd_valid[signal - 1])
        signal_line[start_idx] = curr_sig
        macd_hist[start_idx] = macd_line[start_idx] - curr_sig
        for i in range(start_idx + 1, len(prices)):
            if macd_line[i] is not None:
                curr_sig = alpha * macd_line[i] + (1 - alpha) * curr_sig
                signal_line[i] = curr_sig
                macd_hist[i] = macd_line[i] - curr_sig
    return macd_line, signal_line, macd_hist

def calculate_stochastic(candles: list[Candle], period: int = 14, smooth_k: int = 3) -> tuple[list[float | None], list[float | None]]:
    stoch_k = [None] * len(candles)
    stoch_d = [None] * len(candles)
    if len(candles) < period:
        return stoch_k, stoch_d
        
    raw_k = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        lows = [c.low for c in window]
        highs = [c.high for c in window]
        min_low = min(lows)
        max_high = max(highs)
        diff = max_high - min_low
        if diff > 0:
            raw_k[i] = 100 * (candles[i].close - min_low) / diff
        else:
            raw_k[i] = 50.0
            
    # smooth K to get final K (using 3-period SMA)
    for i in range(period + 1, len(candles)):
        window_k = [x for x in raw_k[i - 2: i + 1] if x is not None]
        if len(window_k) == 3:
            stoch_k[i] = sum(window_k) / 3
            
    # smooth D to get D (3-period SMA of K)
    for i in range(period + 3, len(candles)):
        window_d = [x for x in stoch_k[i - 2: i + 1] if x is not None]
        if len(window_d) == 3:
            stoch_d[i] = sum(window_d) / 3
            
    return stoch_k, stoch_d

def calculate_donchian(candles: list[Candle], period: int = 20) -> tuple[list[float | None], list[float | None]]:
    upper = [None] * len(candles)
    lower = [None] * len(candles)
    if len(candles) < period:
        return upper, lower
    for i in range(period, len(candles)):
        window = candles[i - period : i]  # excludes current candle high/low as per breakout rules
        upper[i] = max(c.high for c in window)
        lower[i] = min(c.low for c in window)
    return upper, lower

def calculate_supertrend(candles: list[Candle], period: int = 10, multiplier: float = 3.0) -> tuple[list[int], list[float]]:
    # trend: 1 = UP, -1 = DOWN
    trend = [1] * len(candles)
    supertrend = [0.0] * len(candles)
    
    atr_values = calculate_atr(candles, period)
    if len(candles) <= period:
        return trend, supertrend
        
    upper_band = [0.0] * len(candles)
    lower_band = [0.0] * len(candles)
    
    for i in range(1, len(candles)):
        c = candles[i]
        curr_atr = atr_values[i] if atr_values[i] is not None else (c.high - c.low)
        hl2 = (c.high + c.low) / 2
        basic_upper = hl2 + multiplier * curr_atr
        basic_lower = hl2 - multiplier * curr_atr
        
        # calculate upper band
        if basic_upper < upper_band[i-1] or candles[i-1].close > upper_band[i-1]:
            upper_band[i] = basic_upper
        else:
            upper_band[i] = upper_band[i-1]
            
        # calculate lower band
        if basic_lower > lower_band[i-1] or candles[i-1].close < lower_band[i-1]:
            lower_band[i] = basic_lower
        else:
            lower_band[i] = lower_band[i-1]
            
        # determine trend
        if i >= period:
            if trend[i-1] == 1 and c.close < lower_band[i]:
                trend[i] = -1
                supertrend[i] = upper_band[i]
            elif trend[i-1] == -1 and c.close > upper_band[i]:
                trend[i] = 1
                supertrend[i] = lower_band[i]
            else:
                trend[i] = trend[i-1]
                supertrend[i] = lower_band[i] if trend[i] == 1 else upper_band[i]
    return trend, supertrend

def calculate_vwap(candles: list[Candle]) -> list[float]:
    vwap = []
    current_day = None
    pv_sum = 0.0
    vol_sum = 0.0
    for c in candles:
        day = datetime.fromtimestamp(c.open_time_ms / 1000, tz=timezone.utc).astimezone(TAIPEI).date()
        if day != current_day:
            current_day = day
            pv_sum = 0.0
            vol_sum = 0.0
        typical_price = (c.high + c.low + c.close) / 3
        pv_sum += typical_price * c.volume
        vol_sum += c.volume
        vwap.append(pv_sum / vol_sum if vol_sum > 0 else c.close)
    return vwap

# ══════════════════════════════════════════════════════════════════════════════
# MAIN PORTFOLIO DECISION GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_winrate_optimized_portfolio_decision(
    candles: list[Candle],
    today_net: float,
    cooldown_until: dict[str, float],
    equity_usdc: float = 150.0,
) -> LiveRouterDecision | None:
    """Generate the portfolio trading decision from the latest candles."""
    if len(candles) < 130:
        logger.warning("portfolio_insufficient_candles", count=len(candles))
        return None

    prices = [c.close for c in candles]
    idx = len(candles) - 1
    c_time = datetime.fromtimestamp(candles[idx].open_time_ms / 1000, tz=timezone.utc).astimezone(TAIPEI)

    # 1. Indicator Precalculation
    ema_fast_1m = calculate_ema(prices, 5)
    ema_slow_1m = calculate_ema(prices, 20)
    ema_trend_50 = calculate_ema(prices, 50)
    
    bb_upper, _, bb_lower = calculate_bollinger_bands(prices, 20, 2.0)
    rsi = calculate_rsi(prices, 14)
    _, _, macd_hist = calculate_macd(prices, 12, 26, 9)
    stoch_k, stoch_d = calculate_stochastic(candles, 14, 3)
    donchian_upper, donchian_lower = calculate_donchian(candles, 20)
    st_trend, _ = calculate_supertrend(candles, 10, 3.0)
    
    vwap = calculate_vwap(candles)
    atr = calculate_atr(candles, 14)
    volume_sma = calculate_ema([c.volume for c in candles], 20)
    
    # 5m trend approximation for MTF
    ema_fast_5m = calculate_ema(prices, 60)
    ema_slow_5m = calculate_ema(prices, 130)

    # 2. State & Volatility Classification
    # 14-period ATR percentile (288-candle rolling window)
    atr_window = 288
    atr_percentile = 0.5
    if idx >= atr_window:
        window_atr = [x for x in atr[idx - atr_window + 1 : idx + 1] if x is not None]
        if window_atr:
            curr_atr = atr[idx]
            below = sum(1 for x in window_atr if x < curr_atr)
            atr_percentile = below / len(window_atr)

    vol = "normal"
    if atr_percentile < 0.25:
        vol = "low"
    elif atr_percentile > 0.80:
        vol = "high"

    # Trend state classification using 50-period EMA slope
    trend = "range"
    curr_ema_slow = ema_slow_1m[idx]
    prev_ema_slow = ema_slow_1m[idx - 20] if idx >= 20 else curr_ema_slow
    curr_atr = atr[idx] if atr[idx] is not None and atr[idx] > 0 else 1.0
    slope = (curr_ema_slow - prev_ema_slow) / curr_atr if curr_ema_slow is not None and prev_ema_slow is not None else 0.0

    if prices[idx] > ema_trend_50[idx] and slope > 0.08:
        trend = "up"
    elif prices[idx] < ema_trend_50[idx] and slope < -0.08:
        trend = "down"

    # Volume and candle ratios for filters
    curr_vol_sma = volume_sma[idx] if volume_sma[idx] is not None and volume_sma[idx] > 0 else 1.0
    vol_ratio = candles[idx].volume / curr_vol_sma
    candle_range = candles[idx].high - candles[idx].low
    body = abs(candles[idx].close - candles[idx].open)
    body_ratio = body / candle_range if candle_range > 0 else 0.0

    # MTF state
    mtf_bullish = ema_fast_5m[idx] > ema_slow_5m[idx] if ema_fast_5m[idx] is not None and ema_slow_5m[idx] is not None else False
    mtf_bearish = ema_fast_5m[idx] < ema_slow_5m[idx] if ema_fast_5m[idx] is not None and ema_slow_5m[idx] is not None else False

    # 3. Dynamic Decision Flow Helpers
    def _is_cooldown(strat: str) -> bool:
        """Check if strategy is in real-time cooldown."""
        return time.time() < cooldown_until.get(strat, 0.0)

    def _get_tp_sl(base_tp_pct: float, base_sl_pct: float, direction: str, apply_adaptive: bool = True) -> tuple[float, float]:
        """Compute adaptive take profit and stop loss prices."""
        tp_pct = base_tp_pct
        sl_pct = base_sl_pct
        if apply_adaptive:
            if vol == "high":
                tp_pct = base_tp_pct * 1.5
                sl_pct = base_sl_pct * 0.80
            elif vol == "low":
                tp_pct = base_tp_pct * 0.75
            if trend in ("up", "down"):
                tp_pct *= 1.20

        entry = candles[idx].close
        if direction == "LONG":
            return entry * (1 + tp_pct), entry * (1 - sl_pct)
        else:
            return entry * (1 - tp_pct), entry * (1 + sl_pct)

    logger.info(
        "portfolio_features",
        time=c_time.strftime("%H:%M"),
        vol=vol,
        trend=trend,
        vol_ratio=round(vol_ratio, 2),
        body_ratio=round(body_ratio, 2),
        rsi=round(rsi[idx], 1) if rsi[idx] is not None else None,
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STRATEGY EVALUATION
    # ══════════════════════════════════════════════════════════════════════════

    # ── Strategy 1: Bollinger Bands + RSI (Low-Vol Range) ──
    if trend == "range" and vol == "low":
        strat_key = "S1_BB_RSI"
        if not _is_cooldown(strat_key) and vol_ratio >= 0.35 and body_ratio >= 0.20:
            if prices[idx] <= bb_lower[idx] and rsi[idx] is not None and rsi[idx] < 30:
                tp, sl = _get_tp_sl(0.0005, 0.0020, "LONG")
                return _make_decision("PLAN_LONG", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])
            elif prices[idx] >= bb_upper[idx] and rsi[idx] is not None and rsi[idx] > 70:
                tp, sl = _get_tp_sl(0.0005, 0.0020, "SHORT")
                return _make_decision("PLAN_SHORT", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])

    # ── Strategy 2: SuperTrend + VWAP (High-Confidence Trend Follow) ──
    if trend in ("up", "down") and vol != "low":
        strat_key = "S2_SuperTrend"
        if not _is_cooldown(strat_key) and vol_ratio >= 0.35:
            is_bullish = st_trend[idx] == 1 and prices[idx] > vwap[idx]
            is_bearish = st_trend[idx] == -1 and prices[idx] < vwap[idx]
            
            crossover = ema_fast_1m[idx - 1] <= ema_slow_1m[idx - 1] and ema_fast_1m[idx] > ema_slow_1m[idx] if ema_fast_1m[idx-1] is not None else False
            crossunder = ema_fast_1m[idx - 1] >= ema_slow_1m[idx - 1] and ema_fast_1m[idx] < ema_slow_1m[idx] if ema_fast_1m[idx-1] is not None else False

            if is_bullish and crossover and mtf_bullish:
                tp, sl = _get_tp_sl(0.0015, 0.0020, "LONG")
                return _make_decision("PLAN_LONG", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])
            elif is_bearish and crossunder and mtf_bearish:
                tp, sl = _get_tp_sl(0.0015, 0.0020, "SHORT")
                return _make_decision("PLAN_SHORT", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])

    # ── Strategy 3: EMA Pullback + MACD (Trend Pullback) ──
    if trend in ("up", "down") and vol == "normal":
        strat_key = "S3_EMA_MACD"
        if not _is_cooldown(strat_key) and vol_ratio >= 0.35:
            is_uptrend = prices[idx] > ema_trend_50[idx]
            is_downtrend = prices[idx] < ema_trend_50[idx]
            
            crossed_up = macd_hist[idx - 1] <= 0 and macd_hist[idx] > 0 if macd_hist[idx - 1] is not None else False
            crossed_dn = macd_hist[idx - 1] >= 0 and macd_hist[idx] < 0 if macd_hist[idx - 1] is not None else False

            if is_uptrend and candles[idx].low <= ema_slow_1m[idx] and crossed_up and mtf_bullish:
                tp, sl = _get_tp_sl(0.0015, 0.0020, "LONG")
                return _make_decision("PLAN_LONG", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])
            elif is_downtrend and candles[idx].high >= ema_slow_1m[idx] and crossed_dn and mtf_bearish:
                tp, sl = _get_tp_sl(0.0015, 0.0020, "SHORT")
                return _make_decision("PLAN_SHORT", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])

    # ── Strategy 4: Donchian Breakout (Explosive Breakout) ──
    if vol == "high":
        strat_key = "S4_Donchian"
        if not _is_cooldown(strat_key):
            high_vol = candles[idx].volume > 2.5 * curr_vol_sma
            strong_body = body_ratio >= 0.40
            
            breaks_upper = prices[idx] > donchian_upper[idx] + 0.3 * curr_atr if donchian_upper[idx] is not None else False
            breaks_lower = prices[idx] < donchian_lower[idx] - 0.3 * curr_atr if donchian_lower[idx] is not None else False

            if high_vol and strong_body and breaks_upper:
                tp, sl = _get_tp_sl(0.0020, 0.0010, "LONG", apply_adaptive=False)
                return _make_decision("PLAN_LONG", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])
            elif high_vol and strong_body and breaks_lower:
                tp, sl = _get_tp_sl(0.0020, 0.0010, "SHORT", apply_adaptive=False)
                return _make_decision("PLAN_SHORT", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])

    # ── Strategy 5: Stochastic Reversion (Wide Normal Range) ──
    if trend == "range" and vol == "normal":
        strat_key = "S5_Stoch"
        if not _is_cooldown(strat_key) and vol_ratio >= 0.35 and body_ratio >= 0.20:
            crossed_up = stoch_k[idx - 1] <= stoch_d[idx - 1] and stoch_k[idx] > stoch_d[idx] if stoch_k[idx-1] is not None else False
            crossed_dn = stoch_k[idx - 1] >= stoch_d[idx - 1] and stoch_k[idx] < stoch_d[idx] if stoch_k[idx-1] is not None else False

            if crossed_up and stoch_d[idx] is not None and stoch_d[idx] < 20:
                tp, sl = _get_tp_sl(0.0015, 0.0015, "LONG")
                return _make_decision("PLAN_LONG", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])
            elif crossed_dn and stoch_d[idx] is not None and stoch_d[idx] > 80:
                tp, sl = _get_tp_sl(0.0015, 0.0015, "SHORT")
                return _make_decision("PLAN_SHORT", strat_key, vol, trend, tp, sl, rsi[idx], curr_atr, vwap[idx])

    return None

def _make_decision(
    action: str,
    strat_key: str,
    vol: str,
    trend: str,
    tp: float,
    sl: float,
    rsi_val: float | None,
    atr_val: float | None,
    vwap_val: float | None,
) -> LiveRouterDecision:
    """Helper to pack decision and signal plan."""
    signal = SignalPlan(
        action=action,
        confidence=90,
        score=95,
        symbol="ETHUSDC",
        price=tp / (1.0015) if action == "PLAN_LONG" else tp / (0.9985),  # approx reference
        rsi=rsi_val,
        atr=atr_val,
        support=None,
        vwap=vwap_val,
        entries=[],
        entry_weights=[],
        stop_loss=sl,
        take_profits=[tp],
        planned_notional_usdc=1000.0,  # Dynamically allocate $1000 notional (approx 14 USDC margin @ 70x leverage)
        planned_margin_usdc=14.3,
        planned_qty=0.0,
        risk_amount_usdc=0.0,
        sizing_mode="core",
        leverage_cap=70.0,  # Capped at 70x leverage on Binance Testnet
        reasons=[f"Win-Rate Optimized Portfolio trigger {strat_key}"],
        risk_notes=[f"vol={vol}", f"trend={trend}"],
    )
    return LiveRouterDecision(
        signal=signal,
        strategy=strat_key,
        regime=trend,
        risk_mode=vol,
        market_playbook=strat_key,
        allocator_state="active",
        allocator_profile="winrate_optimized",
        allocator_scale=1.0,
        max_holding_bars=48,
    )
