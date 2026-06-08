"""Unit tests for the entry trend guard (evaluate_entry_trend_guard).

The live preset only enables mean-reversion (S1/S5), which fire on bars the
regime classifier calls ``range``. But a soft downtrend (price below EMA50,
EMA20 slope negative) is often still labelled ``range``, so the dip-buyer goes
LONG into a dump (Run 4/5 of 2026-06-08). This gate refuses a LONG below a
downward EMA50 and a SHORT above an upward EMA50. We monkeypatch build_features
to drive each branch deterministically.
"""
import src.gridbot.strategy.wildcat_live as wl
from src.gridbot.strategy.long_pullback import Candle


def _candles(n: int = 60) -> list[Candle]:
    return [
        Candle(open_time_ms=i * 60_000, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)
        for i in range(n)
    ]


def _features(price, ema_trend, ema_slow_now, ema_slow_prev, atr, n):
    """Features dict where index n-1 is 'now' and n-21 is the slope baseline."""
    ema_slow = [ema_slow_prev] * n
    ema_slow[n - 1] = ema_slow_now
    ema_slow[n - 21] = ema_slow_prev
    prices = [price] * n
    return {
        "prices": prices,
        "ema_trend": [ema_trend] * n,
        "ema_slow": ema_slow,
        "atr": [atr] * n,
    }


def test_long_blocked_in_soft_downtrend(monkeypatch):
    n = 60
    # price below EMA50, EMA20 fell 1.0 over 20 bars, atr=10 → slope=-0.1 < -0.03
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features(price=99.0, ema_trend=100.0, ema_slow_now=99.0, ema_slow_prev=100.0, atr=10.0, n=n),
    )
    # last close must be below ema_trend; _to_wildcat_candle uses candle.close,
    # so patch the candle close too via prices is not enough — guard reads raw close.
    candles = _candles(n)
    candles[-1] = Candle(open_time_ms=0, open=99.0, high=99.5, low=98.5, close=99.0, volume=1.0)
    allow, reason = wl.evaluate_entry_trend_guard(candles, "LONG")
    assert allow is False
    assert "不逆勢做多" in reason


def test_short_blocked_in_soft_uptrend(monkeypatch):
    n = 60
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features(price=101.0, ema_trend=100.0, ema_slow_now=101.0, ema_slow_prev=100.0, atr=10.0, n=n),
    )
    candles = _candles(n)
    candles[-1] = Candle(open_time_ms=0, open=101.0, high=101.5, low=100.5, close=101.0, volume=1.0)
    allow, reason = wl.evaluate_entry_trend_guard(candles, "SHORT")
    assert allow is False
    assert "不逆勢做空" in reason


def test_long_allowed_when_slope_flat(monkeypatch):
    n = 60
    # price below EMA50 but slope ~0 (range) → allow.
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features(price=99.0, ema_trend=100.0, ema_slow_now=100.0, ema_slow_prev=100.0, atr=10.0, n=n),
    )
    candles = _candles(n)
    candles[-1] = Candle(open_time_ms=0, open=99.0, high=99.5, low=98.5, close=99.0, volume=1.0)
    allow, reason = wl.evaluate_entry_trend_guard(candles, "LONG")
    assert allow is True
    assert "趨勢一致" in reason


def test_long_allowed_when_price_above_ema(monkeypatch):
    n = 60
    # downward slope but price ABOVE EMA50 → not a falling-knife LONG → allow.
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features(price=101.0, ema_trend=100.0, ema_slow_now=99.0, ema_slow_prev=100.0, atr=10.0, n=n),
    )
    candles = _candles(n)
    candles[-1] = Candle(open_time_ms=0, open=101.0, high=101.5, low=100.5, close=101.0, volume=1.0)
    allow, reason = wl.evaluate_entry_trend_guard(candles, "LONG")
    assert allow is True


def test_short_allowed_in_downtrend(monkeypatch):
    n = 60
    # downtrend favours SHORT (price below EMA50, slope down) → allow SHORT.
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features(price=99.0, ema_trend=100.0, ema_slow_now=99.0, ema_slow_prev=100.0, atr=10.0, n=n),
    )
    candles = _candles(n)
    candles[-1] = Candle(open_time_ms=0, open=99.0, high=99.5, low=98.5, close=99.0, volume=1.0)
    allow, reason = wl.evaluate_entry_trend_guard(candles, "SHORT")
    assert allow is True


def test_allowed_when_insufficient_candles():
    allow, reason = wl.evaluate_entry_trend_guard(_candles(10), "LONG")
    assert allow is True
    assert "K 線不足" in reason
