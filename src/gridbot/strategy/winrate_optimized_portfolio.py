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

STRATEGY_SCORE_PROFILE = {
    "S4_Donchian": (96, 92),
    "S2_SuperTrend": (92, 88),
    "S3_EMA_MACD": (88, 84),
    "S5_Stoch": (84, 80),
    "S1_BB_RSI": (78, 74),
}

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
    min_volume_ratio: float = 0.35,
    trigger_lookback_bars: int = 3,
    donchian_volume_multiplier: float = 2.5,
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

    candidates: list[tuple[int, str, LiveRouterDecision]] = []

    def _profile_score(strat_key: str, direction: str) -> int:
        long_score, short_score = STRATEGY_SCORE_PROFILE.get(strat_key, (80, 80))
        return long_score if direction == "LONG" else short_score

    def _add_candidate(
        direction: str,
        strat_key: str,
        entry: float,
        tp: float,
        sl: float,
    ) -> None:
        score = _profile_score(strat_key, direction)
        action = "PLAN_LONG" if direction == "LONG" else "PLAN_SHORT"
        candidates.append(
            (
                score,
                strat_key,
                _make_decision(action, strat_key, vol, trend, entry, tp, sl, rsi[idx], curr_atr, vwap[idx]),
            )
        )

    # ── Strategy 1: Bollinger Bands + RSI (Low-Vol Range) ──
    if trend == "range" and vol == "low":
        strat_key = "S1_BB_RSI"
        if not _is_cooldown(strat_key) and vol_ratio >= min_volume_ratio and body_ratio >= 0.20:
            if prices[idx] <= bb_lower[idx] and rsi[idx] is not None and rsi[idx] < 30:
                tp, sl = _get_tp_sl(0.0005, 0.0020, "LONG")
                _add_candidate("LONG", strat_key, prices[idx], tp, sl)
            elif prices[idx] >= bb_upper[idx] and rsi[idx] is not None and rsi[idx] > 70:
                tp, sl = _get_tp_sl(0.0005, 0.0020, "SHORT")
                _add_candidate("SHORT", strat_key, prices[idx], tp, sl)

    # ── Strategy 2: SuperTrend + VWAP (High-Confidence Trend Follow) ──
    if trend in ("up", "down") and vol != "low":
        strat_key = "S2_SuperTrend"
        if not _is_cooldown(strat_key) and vol_ratio >= min_volume_ratio:
            is_bullish = st_trend[idx] == 1 and prices[idx] > vwap[idx]
            is_bearish = st_trend[idx] == -1 and prices[idx] < vwap[idx]
            
            crossover = _ema_crossed_recently(ema_fast_1m, ema_slow_1m, idx, "up", trigger_lookback_bars)
            crossunder = _ema_crossed_recently(ema_fast_1m, ema_slow_1m, idx, "down", trigger_lookback_bars)
            continuation_long = _ema_continuation_confirmed(ema_fast_1m, ema_slow_1m, prices, candles, idx, "long", body_ratio)
            continuation_short = _ema_continuation_confirmed(ema_fast_1m, ema_slow_1m, prices, candles, idx, "short", body_ratio)

            if is_bullish and mtf_bullish and (crossover or continuation_long):
                tp, sl = _get_tp_sl(0.0015, 0.0020, "LONG")
                _add_candidate("LONG", strat_key, prices[idx], tp, sl)
            elif is_bearish and mtf_bearish and (crossunder or continuation_short):
                tp, sl = _get_tp_sl(0.0015, 0.0020, "SHORT")
                _add_candidate("SHORT", strat_key, prices[idx], tp, sl)

    # ── Strategy 3: EMA Pullback + MACD (Trend Pullback) ──
    if trend in ("up", "down") and vol == "normal":
        strat_key = "S3_EMA_MACD"
        if not _is_cooldown(strat_key) and vol_ratio >= min_volume_ratio:
            is_uptrend = prices[idx] > ema_trend_50[idx]
            is_downtrend = prices[idx] < ema_trend_50[idx]
            
            crossed_up = _zero_crossed_recently(macd_hist, idx, "up", trigger_lookback_bars)
            crossed_dn = _zero_crossed_recently(macd_hist, idx, "down", trigger_lookback_bars)

            if is_uptrend and candles[idx].low <= ema_slow_1m[idx] and crossed_up and mtf_bullish:
                tp, sl = _get_tp_sl(0.0015, 0.0020, "LONG")
                _add_candidate("LONG", strat_key, prices[idx], tp, sl)
            elif is_downtrend and candles[idx].high >= ema_slow_1m[idx] and crossed_dn and mtf_bearish:
                tp, sl = _get_tp_sl(0.0015, 0.0020, "SHORT")
                _add_candidate("SHORT", strat_key, prices[idx], tp, sl)

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
                _add_candidate("LONG", strat_key, prices[idx], tp, sl)
            elif high_vol and strong_body and breaks_lower:
                tp, sl = _get_tp_sl(0.0020, 0.0010, "SHORT", apply_adaptive=False)
                _add_candidate("SHORT", strat_key, prices[idx], tp, sl)

    # ── Strategy 5: Stochastic Reversion (Wide Normal Range) ──
    if trend == "range" and vol == "normal":
        strat_key = "S5_Stoch"
        if not _is_cooldown(strat_key) and vol_ratio >= min_volume_ratio and body_ratio >= 0.20:
            crossed_up = stoch_k[idx - 1] <= stoch_d[idx - 1] and stoch_k[idx] > stoch_d[idx] if stoch_k[idx-1] is not None else False
            crossed_dn = stoch_k[idx - 1] >= stoch_d[idx - 1] and stoch_k[idx] < stoch_d[idx] if stoch_k[idx-1] is not None else False

            if crossed_up and stoch_d[idx] is not None and stoch_d[idx] < 20:
                tp, sl = _get_tp_sl(0.0015, 0.0015, "LONG")
                _add_candidate("LONG", strat_key, prices[idx], tp, sl)
            elif crossed_dn and stoch_d[idx] is not None and stoch_d[idx] > 80:
                tp, sl = _get_tp_sl(0.0015, 0.0015, "SHORT")
                _add_candidate("SHORT", strat_key, prices[idx], tp, sl)

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_score, best_strategy, best_decision = candidates[0]
        logger.info(
            "portfolio_score_selected",
            selected=best_strategy,
            score=best_score,
            candidates=[
                {
                    "strategy": strategy,
                    "score": score,
                    "action": decision.signal.action,
                }
                for score, strategy, decision in candidates
            ],
        )
        return best_decision

    return None


def explain_winrate_optimized_portfolio_no_signal(
    candles: list[Candle],
    today_net: float,
    cooldown_until: dict[str, float],
    equity_usdc: float = 150.0,
    min_volume_ratio: float = 0.35,
    trigger_lookback_bars: int = 3,
    donchian_volume_multiplier: float = 2.5,
) -> list[str]:
    """Explain the most likely reason the portfolio did not emit a live signal."""
    del today_net, equity_usdc
    if len(candles) < 130:
        return [f"資料不足：目前只有 {len(candles)} 根 K 線，至少需要 130 根"]

    status = describe_winrate_optimized_portfolio_status(
        candles=candles,
        today_net=0.0,
        cooldown_until=cooldown_until,
        min_volume_ratio=min_volume_ratio,
        trigger_lookback_bars=trigger_lookback_bars,
        donchian_volume_multiplier=donchian_volume_multiplier,
    )
    summary = status.get("summary") or {}
    reasons = [
        "市場摘要："
        f"trend={summary.get('trend')} / "
        f"vol={summary.get('vol')} / "
        f"vol_ratio={float(summary.get('vol_ratio') or 0):.2f} / "
        f"body_ratio={float(summary.get('body_ratio') or 0):.2f}",
    ]
    ready_candidates = status.get("ready_candidates") or []
    if ready_candidates:
        reasons.append(
            "可交易候選："
            + "；".join(
                f"{row.get('key')} {row.get('direction')} score={int(row.get('score') or 0)}"
                for row in ready_candidates
            )
        )
    else:
        reasons.append("目前沒有任何 S1~S5 通過開單門檻")
    for row in status.get("strategies") or []:
        reasons.append(
            f"{row.get('key')}[{row.get('status')}] "
            f"level={int(row.get('level_score') or 0)}：{row.get('reason')}"
        )
    return reasons

    prices = [c.close for c in candles]
    idx = len(candles) - 1

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
    ema_fast_5m = calculate_ema(prices, 60)
    ema_slow_5m = calculate_ema(prices, 130)

    atr_window = 288
    atr_percentile = 0.5
    if idx >= atr_window:
        window_atr = [x for x in atr[idx - atr_window + 1 : idx + 1] if x is not None]
        if window_atr and atr[idx] is not None:
            below = sum(1 for x in window_atr if x < atr[idx])
            atr_percentile = below / len(window_atr)

    vol = "normal"
    if atr_percentile < 0.25:
        vol = "low"
    elif atr_percentile > 0.80:
        vol = "high"

    trend = "range"
    curr_ema_slow = ema_slow_1m[idx]
    prev_ema_slow = ema_slow_1m[idx - 20] if idx >= 20 else curr_ema_slow
    curr_atr = atr[idx] if atr[idx] is not None and atr[idx] > 0 else 1.0
    slope = (curr_ema_slow - prev_ema_slow) / curr_atr if curr_ema_slow is not None and prev_ema_slow is not None else 0.0
    if ema_trend_50[idx] is not None and prices[idx] > ema_trend_50[idx] and slope > 0.08:
        trend = "up"
    elif ema_trend_50[idx] is not None and prices[idx] < ema_trend_50[idx] and slope < -0.08:
        trend = "down"

    curr_vol_sma = volume_sma[idx] if volume_sma[idx] is not None and volume_sma[idx] > 0 else 1.0
    vol_ratio = candles[idx].volume / curr_vol_sma
    candle_range = candles[idx].high - candles[idx].low
    body = abs(candles[idx].close - candles[idx].open)
    body_ratio = body / candle_range if candle_range > 0 else 0.0
    mtf_bullish = ema_fast_5m[idx] > ema_slow_5m[idx] if ema_fast_5m[idx] is not None and ema_slow_5m[idx] is not None else False
    mtf_bearish = ema_fast_5m[idx] < ema_slow_5m[idx] if ema_fast_5m[idx] is not None and ema_slow_5m[idx] is not None else False

    def _is_cooldown(strat: str) -> bool:
        return time.time() < cooldown_until.get(strat, 0.0)

    reasons = [
        f"市場摘要：trend={trend} / vol={vol} / vol_ratio={vol_ratio:.2f} / body_ratio={body_ratio:.2f}",
    ]

    if trend == "range" and vol == "low":
        if _is_cooldown("S1_BB_RSI"):
            reasons.append("S1_BB_RSI 冷卻中")
        elif vol_ratio < min_volume_ratio:
            reasons.append(f"S1_BB_RSI：成交量不足，vol_ratio={vol_ratio:.2f} < {min_volume_ratio:.2f}")
        elif body_ratio < 0.20:
            reasons.append(f"S1_BB_RSI：K 線實體太小，body_ratio={body_ratio:.2f} < 0.20")
        elif rsi[idx] is None or bb_lower[idx] is None or bb_upper[idx] is None:
            reasons.append("S1_BB_RSI：指標尚未就緒")
        elif prices[idx] > bb_lower[idx] and prices[idx] < bb_upper[idx]:
            reasons.append("S1_BB_RSI：價格尚未碰到布林上下緣")
        else:
            reasons.append(f"S1_BB_RSI：RSI 未進入極端區，目前 RSI={rsi[idx]:.1f}")
        return reasons

    if trend in ("up", "down") and vol != "low":
        if vol == "normal":
            if _is_cooldown("S3_EMA_MACD"):
                reasons.append("S3_EMA_MACD 冷卻中")
            elif vol_ratio < min_volume_ratio:
                reasons.append(f"S3_EMA_MACD：成交量不足，vol_ratio={vol_ratio:.2f} < {min_volume_ratio:.2f}")
            elif trend == "up" and not mtf_bullish:
                reasons.append("S3_EMA_MACD：5m 趨勢尚未轉多")
            elif trend == "down" and not mtf_bearish:
                reasons.append("S3_EMA_MACD：5m 趨勢尚未轉空")
            elif trend == "up" and ema_slow_1m[idx] is not None and candles[idx].low > ema_slow_1m[idx]:
                reasons.append("S3_EMA_MACD：還沒回踩到 EMA20")
            elif trend == "down" and ema_slow_1m[idx] is not None and candles[idx].high < ema_slow_1m[idx]:
                reasons.append("S3_EMA_MACD：還沒反彈到 EMA20")
            elif macd_hist[idx - 1] is None or macd_hist[idx] is None:
                reasons.append("S3_EMA_MACD：MACD 指標尚未就緒")
            elif trend == "up" and not _zero_crossed_recently(macd_hist, idx, "up", trigger_lookback_bars):
                reasons.append(f"S3_EMA_MACD：最近 {trigger_lookback_bars} 根還沒出現 MACD 翻多")
            elif trend == "down" and not _zero_crossed_recently(macd_hist, idx, "down", trigger_lookback_bars):
                reasons.append(f"S3_EMA_MACD：最近 {trigger_lookback_bars} 根還沒出現 MACD 翻空")
            else:
                reasons.append("S3_EMA_MACD：條件接近，但本輪未同時滿足回踩與動能翻轉")

        if _is_cooldown("S2_SuperTrend"):
            reasons.append("S2_SuperTrend 冷卻中")
        elif vol_ratio < min_volume_ratio:
            reasons.append(f"S2_SuperTrend：成交量不足，vol_ratio={vol_ratio:.2f} < {min_volume_ratio:.2f}")
        elif trend == "up" and not mtf_bullish:
            reasons.append("S2_SuperTrend：5m 趨勢尚未轉多")
        elif trend == "down" and not mtf_bearish:
            reasons.append("S2_SuperTrend：5m 趨勢尚未轉空")
        elif trend == "up" and not (st_trend[idx] == 1 and prices[idx] > vwap[idx]):
            reasons.append("S2_SuperTrend：SuperTrend / VWAP 多頭條件未齊")
        elif trend == "down" and not (st_trend[idx] == -1 and prices[idx] < vwap[idx]):
            reasons.append("S2_SuperTrend：SuperTrend / VWAP 空頭條件未齊")
        elif ema_fast_1m[idx - 1] is None or ema_slow_1m[idx - 1] is None or ema_fast_1m[idx] is None or ema_slow_1m[idx] is None:
            reasons.append("S2_SuperTrend：EMA 指標尚未就緒")
        elif trend == "up" and not (
            _ema_crossed_recently(ema_fast_1m, ema_slow_1m, idx, "up", trigger_lookback_bars)
            or _ema_continuation_confirmed(ema_fast_1m, ema_slow_1m, prices, candles, idx, "long", body_ratio)
        ):
            reasons.append(f"S2_SuperTrend：最近 {trigger_lookback_bars} 根沒有 EMA5/EMA20 黃金交叉或延續確認")
        elif trend == "down" and not (
            _ema_crossed_recently(ema_fast_1m, ema_slow_1m, idx, "down", trigger_lookback_bars)
            or _ema_continuation_confirmed(ema_fast_1m, ema_slow_1m, prices, candles, idx, "short", body_ratio)
        ):
            reasons.append(f"S2_SuperTrend：最近 {trigger_lookback_bars} 根沒有 EMA5/EMA20 死亡交叉或延續確認")
        else:
            reasons.append("S2_SuperTrend：趨勢存在，但本輪沒有交叉觸發")
        return reasons

    if vol == "high":
        if _is_cooldown("S4_Donchian"):
            reasons.append("S4_Donchian 冷卻中")
        else:
            high_vol = donchian_volume_multiplier <= 0 or candles[idx].volume > donchian_volume_multiplier * curr_vol_sma
            strong_body = body_ratio >= 0.40
            breaks_upper = prices[idx] > donchian_upper[idx] + 0.3 * curr_atr if donchian_upper[idx] is not None else False
            breaks_lower = prices[idx] < donchian_lower[idx] - 0.3 * curr_atr if donchian_lower[idx] is not None else False
            if not high_vol:
                reasons.append(
                    f"S4_Donchian：爆量不足，目前 volume={candles[idx].volume:.2f}，"
                    f"門檻={donchian_volume_multiplier * curr_vol_sma:.2f}"
                )
            elif not strong_body:
                reasons.append(f"S4_Donchian：K 線實體不夠強，body_ratio={body_ratio:.2f} < 0.40")
            elif not breaks_upper and not breaks_lower:
                reasons.append("S4_Donchian：尚未突破 Donchian 通道")
            else:
                reasons.append("S4_Donchian：突破條件接近，但方向確認尚未完成")
        return reasons

    if trend == "range" and vol == "normal":
        if _is_cooldown("S5_Stoch"):
            reasons.append("S5_Stoch 冷卻中")
        elif vol_ratio < min_volume_ratio:
            reasons.append(f"S5_Stoch：成交量不足，vol_ratio={vol_ratio:.2f} < {min_volume_ratio:.2f}")
        elif body_ratio < 0.20:
            reasons.append(f"S5_Stoch：K 線實體太小，body_ratio={body_ratio:.2f} < 0.20")
        elif any(x is None for x in (stoch_k[idx - 1], stoch_d[idx - 1], stoch_k[idx], stoch_d[idx])):
            reasons.append("S5_Stoch：隨機指標尚未就緒")
        elif not (
            (stoch_k[idx - 1] <= stoch_d[idx - 1] and stoch_k[idx] > stoch_d[idx] and stoch_d[idx] < 20)
            or (stoch_k[idx - 1] >= stoch_d[idx - 1] and stoch_k[idx] < stoch_d[idx] and stoch_d[idx] > 80)
        ):
            reasons.append(
                f"S5_Stoch：尚未出現超買/超賣反轉，目前 K/D={stoch_k[idx]:.1f}/{stoch_d[idx]:.1f}"
            )
        else:
            reasons.append("S5_Stoch：區間反轉條件接近，但交叉確認不足")
        return reasons

    reasons.append("目前市場狀態沒有對應的優先策略 setup")
    return reasons


def describe_winrate_optimized_portfolio_status(
    candles: list[Candle],
    today_net: float,
    cooldown_until: dict[str, float],
    equity_usdc: float = 150.0,
    min_volume_ratio: float = 0.35,
    trigger_lookback_bars: int = 3,
    donchian_volume_multiplier: float = 2.5,
) -> dict:
    """Return a per-strategy S1-S5 status table for Telegram diagnostics."""
    del today_net, equity_usdc
    if len(candles) < 130:
        return {
            "ready": False,
            "summary": {"reason": f"資料不足：目前只有 {len(candles)} 根 K 線，至少需要 130 根"},
            "strategies": [],
        }

    prices = [c.close for c in candles]
    idx = len(candles) - 1

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
    ema_fast_5m = calculate_ema(prices, 60)
    ema_slow_5m = calculate_ema(prices, 130)

    atr_window = 288
    atr_percentile = 0.5
    if idx >= atr_window:
        window_atr = [x for x in atr[idx - atr_window + 1 : idx + 1] if x is not None]
        if window_atr and atr[idx] is not None:
            atr_percentile = sum(1 for x in window_atr if x < atr[idx]) / len(window_atr)

    vol = "normal"
    if atr_percentile < 0.25:
        vol = "low"
    elif atr_percentile > 0.80:
        vol = "high"

    trend = "range"
    curr_ema_slow = ema_slow_1m[idx]
    prev_ema_slow = ema_slow_1m[idx - 20] if idx >= 20 else curr_ema_slow
    curr_atr = atr[idx] if atr[idx] is not None and atr[idx] > 0 else 1.0
    slope = (curr_ema_slow - prev_ema_slow) / curr_atr if curr_ema_slow is not None and prev_ema_slow is not None else 0.0
    if ema_trend_50[idx] is not None and prices[idx] > ema_trend_50[idx] and slope > 0.08:
        trend = "up"
    elif ema_trend_50[idx] is not None and prices[idx] < ema_trend_50[idx] and slope < -0.08:
        trend = "down"

    curr_vol_sma = volume_sma[idx] if volume_sma[idx] is not None and volume_sma[idx] > 0 else 1.0
    vol_ratio = candles[idx].volume / curr_vol_sma
    candle_range = candles[idx].high - candles[idx].low
    body_ratio = abs(candles[idx].close - candles[idx].open) / candle_range if candle_range > 0 else 0.0
    mtf_bullish = ema_fast_5m[idx] > ema_slow_5m[idx] if ema_fast_5m[idx] is not None and ema_slow_5m[idx] is not None else False
    mtf_bearish = ema_fast_5m[idx] < ema_slow_5m[idx] if ema_fast_5m[idx] is not None and ema_slow_5m[idx] is not None else False

    def _is_cooldown(strat: str) -> bool:
        return time.time() < cooldown_until.get(strat, 0.0)

    def _row(
        key: str,
        name: str,
        regime_match: bool,
        passed: bool,
        direction: str,
        score: int,
        reason: str,
    ) -> dict:
        if _is_cooldown(key):
            return {
                "key": key,
                "name": name,
                "status": "cooldown",
                "direction": "WAIT",
                "score": 0,
                "level_score": score,
                "reason": "策略冷卻中",
            }
        status = "ready" if passed else ("watch" if regime_match else "inactive")
        return {
            "key": key,
            "name": name,
            "status": status,
            "direction": direction if passed else "WAIT",
            "score": score if passed else 0,
            "level_score": score,
            "reason": reason,
        }

    rows = []

    s1_regime = trend == "range" and vol == "low"
    s1_long = prices[idx] <= bb_lower[idx] and rsi[idx] is not None and rsi[idx] < 30 if bb_lower[idx] is not None else False
    s1_short = prices[idx] >= bb_upper[idx] and rsi[idx] is not None and rsi[idx] > 70 if bb_upper[idx] is not None else False
    s1_pass = s1_regime and vol_ratio >= min_volume_ratio and body_ratio >= 0.20 and (s1_long or s1_short)
    rsi_label = f"{rsi[idx]:.1f}" if rsi[idx] is not None else "N/A"
    s1_reason = (
        "布林+RSI 極端反轉成立"
        if s1_pass
        else f"需要 range+low vol；目前 trend={trend}, vol={vol}, RSI={rsi_label}"
    )
    rows.append(_row("S1_BB_RSI", "S1 布林RSI", s1_regime, s1_pass, "LONG" if s1_long else "SHORT", 78, s1_reason))

    s2_regime = trend in ("up", "down") and vol != "low"
    s2_bull = st_trend[idx] == 1 and prices[idx] > vwap[idx] and mtf_bullish
    s2_bear = st_trend[idx] == -1 and prices[idx] < vwap[idx] and mtf_bearish
    s2_cross_long = _ema_crossed_recently(ema_fast_1m, ema_slow_1m, idx, "up", trigger_lookback_bars) or _ema_continuation_confirmed(ema_fast_1m, ema_slow_1m, prices, candles, idx, "long", body_ratio)
    s2_cross_short = _ema_crossed_recently(ema_fast_1m, ema_slow_1m, idx, "down", trigger_lookback_bars) or _ema_continuation_confirmed(ema_fast_1m, ema_slow_1m, prices, candles, idx, "short", body_ratio)
    s2_pass = s2_regime and vol_ratio >= min_volume_ratio and ((s2_bull and s2_cross_long) or (s2_bear and s2_cross_short))
    s2_direction = "LONG" if s2_bull and s2_cross_long else "SHORT"
    s2_reason = "SuperTrend+VWAP+EMA 延續成立" if s2_pass else f"需要趨勢+量能+EMA 觸發；vol_ratio={vol_ratio:.2f}"
    rows.append(_row("S2_SuperTrend", "S2 SuperTrend", s2_regime, s2_pass, s2_direction, 94 if s2_direction == "SHORT" else 92, s2_reason))

    s3_regime = trend in ("up", "down") and vol == "normal"
    s3_long = prices[idx] > ema_trend_50[idx] and candles[idx].low <= ema_slow_1m[idx] and mtf_bullish and _zero_crossed_recently(macd_hist, idx, "up", trigger_lookback_bars) if None not in (ema_trend_50[idx], ema_slow_1m[idx]) else False
    s3_short = prices[idx] < ema_trend_50[idx] and candles[idx].high >= ema_slow_1m[idx] and mtf_bearish and _zero_crossed_recently(macd_hist, idx, "down", trigger_lookback_bars) if None not in (ema_trend_50[idx], ema_slow_1m[idx]) else False
    s3_pass = s3_regime and vol_ratio >= min_volume_ratio and (s3_long or s3_short)
    s3_reason = "EMA20 回踩 + MACD 翻轉成立" if s3_pass else f"需要 normal vol 趨勢回踩；目前 vol={vol}"
    rows.append(_row("S3_EMA_MACD", "S3 EMA/MACD", s3_regime, s3_pass, "LONG" if s3_long else "SHORT", 88, s3_reason))

    s4_regime = vol == "high"
    high_vol = donchian_volume_multiplier <= 0 or candles[idx].volume > donchian_volume_multiplier * curr_vol_sma
    strong_body = body_ratio >= 0.40
    breaks_upper = prices[idx] > donchian_upper[idx] + 0.3 * curr_atr if donchian_upper[idx] is not None else False
    breaks_lower = prices[idx] < donchian_lower[idx] - 0.3 * curr_atr if donchian_lower[idx] is not None else False
    s4_pass = s4_regime and high_vol and strong_body and (breaks_upper or breaks_lower)
    s4_reason = "Donchian 爆量突破成立" if s4_pass else f"需要 high vol + {donchian_volume_multiplier:.1f}x爆量 + 通道突破"
    rows.append(_row("S4_Donchian", "S4 Donchian", s4_regime, s4_pass, "LONG" if breaks_upper else "SHORT", 96, s4_reason))

    s5_regime = trend == "range" and vol == "normal"
    stoch_ready = not any(x is None for x in (stoch_k[idx - 1], stoch_d[idx - 1], stoch_k[idx], stoch_d[idx]))
    s5_long = stoch_ready and stoch_k[idx - 1] <= stoch_d[idx - 1] and stoch_k[idx] > stoch_d[idx] and stoch_d[idx] < 20
    s5_short = stoch_ready and stoch_k[idx - 1] >= stoch_d[idx - 1] and stoch_k[idx] < stoch_d[idx] and stoch_d[idx] > 80
    s5_pass = s5_regime and vol_ratio >= min_volume_ratio and body_ratio >= 0.20 and (s5_long or s5_short)
    s5_reason = "Stoch 超買/超賣反轉成立" if s5_pass else f"需要 range+normal vol；目前 trend={trend}, vol={vol}"
    rows.append(_row("S5_Stoch", "S5 Stoch", s5_regime, s5_pass, "LONG" if s5_long else "SHORT", 84, s5_reason))
    ready_candidates = sorted(
        [row for row in rows if row.get("status") == "ready"],
        key=lambda row: int(row.get("score") or 0),
        reverse=True,
    )

    return {
        "ready": True,
        "summary": {
            "trend": trend,
            "vol": vol,
            "vol_ratio": round(vol_ratio, 2),
            "body_ratio": round(body_ratio, 2),
            "rsi": round(rsi[idx], 1) if rsi[idx] is not None else None,
            "price": prices[idx],
            "vwap": vwap[idx],
            "atr": curr_atr,
            "ready_count": len(ready_candidates),
            "top_candidate": ready_candidates[0] if ready_candidates else None,
        },
        "strategies": rows,
        "ready_candidates": ready_candidates,
    }


def _ema_crossed_recently(
    fast: list[float | None],
    slow: list[float | None],
    idx: int,
    direction: str,
    lookback_bars: int,
) -> bool:
    start = max(1, idx - max(1, int(lookback_bars)) + 1)
    for i in range(start, idx + 1):
        prev_fast, prev_slow = fast[i - 1], slow[i - 1]
        curr_fast, curr_slow = fast[i], slow[i]
        if None in (prev_fast, prev_slow, curr_fast, curr_slow):
            continue
        if direction == "up" and prev_fast <= prev_slow and curr_fast > curr_slow:
            return True
        if direction == "down" and prev_fast >= prev_slow and curr_fast < curr_slow:
            return True
    return False


def _zero_crossed_recently(
    series: list[float | None],
    idx: int,
    direction: str,
    lookback_bars: int,
) -> bool:
    start = max(1, idx - max(1, int(lookback_bars)) + 1)
    for i in range(start, idx + 1):
        prev_value, curr_value = series[i - 1], series[i]
        if prev_value is None or curr_value is None:
            continue
        if direction == "up" and prev_value <= 0 and curr_value > 0:
            return True
        if direction == "down" and prev_value >= 0 and curr_value < 0:
            return True
    return False


def _ema_continuation_confirmed(
    fast: list[float | None],
    slow: list[float | None],
    prices: list[float],
    candles: list[Candle],
    idx: int,
    direction: str,
    body_ratio: float,
) -> bool:
    curr_fast, curr_slow = fast[idx], slow[idx]
    if curr_fast is None or curr_slow is None or body_ratio < 0.35:
        return False
    candle = candles[idx]
    if direction == "long":
        return curr_fast > curr_slow and prices[idx] > curr_fast and candle.close > candle.open
    return curr_fast < curr_slow and prices[idx] < curr_fast and candle.close < candle.open


def _make_decision(
    action: str,
    strat_key: str,
    vol: str,
    trend: str,
    entry_price: float,
    tp: float,
    sl: float,
    rsi_val: float | None,
    atr_val: float | None,
    vwap_val: float | None,
) -> LiveRouterDecision:
    """Helper to pack decision and signal plan."""
    score, confidence = _score_profile(strat_key, action, entry_price, tp)
    signal = SignalPlan(
        action=action,
        confidence=confidence,
        score=score,
        symbol="ETHUSDC",
        price=entry_price,
        rsi=rsi_val,
        atr=atr_val,
        support=None,
        vwap=vwap_val,
        entries=[],
        entry_weights=[],
        stop_loss=sl,
        take_profits=[tp],
        planned_notional_usdc=1000.0,  # Dynamically allocate $1000 notional (approx 10 USDC margin @ 100x leverage)
        planned_margin_usdc=10.0,
        planned_qty=0.0,
        risk_amount_usdc=0.0,
        sizing_mode="core",
        leverage_cap=100.0,  # Capped at 100x leverage on Binance Testnet
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


def _score_profile(strat_key: str, action: str, entry_price: float, take_profit: float) -> tuple[int, int]:
    base_score, base_confidence = STRATEGY_SCORE_PROFILE.get(strat_key, (82, 78))
    if entry_price <= 0 or take_profit <= 0:
        return base_score, base_confidence

    reward_pct = (
        (take_profit - entry_price) / entry_price * 100
        if action == "PLAN_LONG"
        else (entry_price - take_profit) / entry_price * 100
    )
    if reward_pct >= 0.18:
        base_score += 2
        base_confidence += 2
    elif reward_pct < 0.12:
        base_score -= 4
        base_confidence -= 4
    return max(1, min(99, base_score)), max(1, min(99, base_confidence))
