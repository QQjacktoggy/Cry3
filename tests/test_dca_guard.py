"""Unit tests for the DCA risk gate (evaluate_dca_guard).

DCA doubles the position while price moves against us; if the SL then fires the
loss also doubles. The gate must block DCA when the adverse move looks like a
trend or momentum has reversed against the position, rather than a range
pullback. We monkeypatch build_features to drive each branch deterministically.
"""
import src.gridbot.strategy.wildcat_live as wl
from src.gridbot.strategy.long_pullback import Candle


def _candles(n: int = 5) -> list[Candle]:
    return [
        Candle(open_time_ms=i * 60_000, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)
        for i in range(n)
    ]


def _features(trend, k, d, k_prev, d_prev, n):
    """Build a features dict where the last index has the given values."""
    return {
        "trend": ["range"] * (n - 1) + [trend],
        "stoch_k": [k_prev] * (n - 1) + [k],
        "stoch_d": [d_prev] * (n - 1) + [d],
    }


def test_dca_blocked_when_trend_not_range(monkeypatch):
    n = 5
    monkeypatch.setattr(wl, "build_features", lambda raw: _features("up", 50, 50, 50, 50, n))
    allow, reason = wl.evaluate_dca_guard(_candles(n), "SHORT")
    assert allow is False
    assert "trend=up" in reason


def test_dca_blocked_short_on_stoch_golden_cross(monkeypatch):
    # SHORT + bullish (golden) cross: upward momentum strengthening → block.
    n = 5
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features("range", k=60.0, d=55.0, k_prev=50.0, d_prev=55.0, n=n),
    )
    allow, reason = wl.evaluate_dca_guard(_candles(n), "SHORT")
    assert allow is False
    assert "金叉" in reason


def test_dca_blocked_long_on_stoch_death_cross(monkeypatch):
    # LONG + bearish (death) cross: downward momentum strengthening → block.
    n = 5
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features("range", k=40.0, d=45.0, k_prev=50.0, d_prev=45.0, n=n),
    )
    allow, reason = wl.evaluate_dca_guard(_candles(n), "LONG")
    assert allow is False
    assert "死叉" in reason


def test_dca_allowed_range_no_reversal(monkeypatch):
    # range + no adverse cross → allow.
    n = 5
    monkeypatch.setattr(
        wl, "build_features",
        lambda raw: _features("range", k=40.0, d=45.0, k_prev=50.0, d_prev=45.0, n=n),
    )
    # death cross is adverse only for LONG; for SHORT it is favorable → allow.
    allow, reason = wl.evaluate_dca_guard(_candles(n), "SHORT")
    assert allow is True
    assert "range" in reason


def test_dca_blocked_when_insufficient_candles(monkeypatch):
    allow, reason = wl.evaluate_dca_guard([], "SHORT")
    assert allow is False
    assert "K 線不足" in reason
