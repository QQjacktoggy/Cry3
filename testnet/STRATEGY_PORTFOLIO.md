# Win-Rate Optimized Scalp Portfolio — Deployed Strategy Manual

This manual describes the five strategies and five optimized filters currently deployed on the live GCP VM auto-trader.

---

## Deployed Strategies

1. **S1_BB_RSI (Bollinger Bands + RSI Mean Reversion)**
   - **Trigger**: Flat/low-volatility range markets.
   - **LONG**: Close <= Lower Bollinger Band (20, 2.0) AND RSI (14) < 30.
   - **SHORT**: Close >= Upper Bollinger Band (20, 2.0) AND RSI (14) > 70.
   - **TP/SL**: Base TP 0.05%, SL 0.20% (with adaptive adjustments).

2. **S2_SuperTrend (SuperTrend + Daily VWAP Trend Following)**
   - **Trigger**: Normal/high-volatility trending markets.
   - **LONG**: SuperTrend is positive, Close > Daily VWAP, 5m EMA trend is bullish, and 1m EMA crossover (EMA 5 > EMA 20).
   - **SHORT**: SuperTrend is negative, Close < Daily VWAP, 5m EMA trend is bearish, and 1m EMA crossunder (EMA 5 < EMA 20).
   - **TP/SL**: Base TP 0.15%, SL 0.20% (with adaptive adjustments).

3. **S3_EMA_MACD (EMA Pullback + MACD momentum)**
   - **Trigger**: Normal volatility trending pullbacks.
   - **LONG**: Close > EMA 50, 5m EMA trend is bullish, Close pulls back to EMA 20, and MACD histogram crosses above zero.
   - **SHORT**: Close < EMA 50, 5m EMA trend is bearish, Close pulls back to EMA 20, and MACD histogram crosses below zero.
   - **TP/SL**: Base TP 0.15%, SL 0.20% (with adaptive adjustments).

4. **S4_Donchian (Explosive Breakout)**
   - **Trigger**: Extreme high-volatility breakout moves.
   - **LONG**: Close exceeds 20-period Donchian Channel High by > 0.3 ATR, volume > 2.5x SMA, and body ratio > 40%.
   - **SHORT**: Close falls below 20-period Donchian Channel Low by > 0.3 ATR, volume > 2.5x SMA, and body ratio > 40%.
   - **TP/SL**: Fixed TP 0.20%, SL 0.10% (strict 2:1 R:R, no adaptive tuning).

5. **S5_Stoch (Stochastic Reversion)**
   - **Trigger**: Normal volatility range markets.
   - **LONG**: Stochastic %K crosses %D below 20.
   - **SHORT**: Stochastic %K crosses %D above 80.
   - **TP/SL**: Base TP 0.15%, SL 0.15% (with adaptive adjustments).

---

## Active Operational Filters

1. **Volume Gate (0.35x)**
   - Reject entries for mean reversion and standard trend strategies (S1, S2, S3, S5) if current volume is < 35% of the 20-bar average. Keeps bot out of illiquid consolidations.

2. **Multi-Timeframe Confirmation (MTF)**
   - Trend following strategies (S2, S3) must align with the 5m EMA 12/26 trend background (approximated by EMA 60/130 on the 1m chart).

3. **Candle Body strength filter (0.20)**
   - S1 and S5 must have body-to-range ratio > 20% to avoid entering on weak indecision dojis.

4. **Per-Strategy Cooldown (5 Minutes)**
   - After a stop loss occurs, that specific strategy is placed on a strict 300-second cooldown timer. Other strategies continue scanning normally.

5. **Adaptive TP/SL Sizing**
   - **High Volatility (ATR percentile > 80%)**: TP = TP * 1.5, SL = SL * 0.8.
   - **Low Volatility (ATR percentile < 25%)**: TP = TP * 0.75.
   - **Trend Crossover (Up/Down trend classification)**: TP = TP * 1.2.
