#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

START_DATE="${START_DATE:-2025-05-14}"
END_DATE="${END_DATE:-2026-05-13}"
LABEL="${LABEL:-router_allocator_v13_trend350_ai_risk_auto_validation_12m}"
PER_SEGMENT_TIMEOUT="${PER_SEGMENT_TIMEOUT:-900}"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

COMMON_ARGS=(
  --mode signal_journal
  --symbol ETHUSDC
  --journal-side router
  --journal-regime-router
  --journal-router-defensive-scale 0.70
  --journal-router-exploratory-scale 0.35
  --journal-regime-allocator
  --journal-allocator-trend-aggressive-scale 3.50
  --journal-allocator-trend-normal-scale 1.00
  --journal-allocator-trend-normal-low-quality-scale 0.35
  --journal-allocator-trend-normal-weak-scale 0.35
  --journal-allocator-short-scale 0.55
  --journal-allocator-short-weak-low-atr-scale 0.25
  --journal-allocator-short-fake-risk-scale 0.05
  --journal-allocator-short-exhaustion-scale 0.30
  --journal-allocator-short-breakdown-scale 1.25
  --journal-allocator-volatility-short-breakdown-scale 0.45
  --journal-allocator-reversion-scale 0.05
  --journal-allocator-weak-pullback-scale 0.30
  --journal-allocator-weak-pullback-normal-scale 0.20
  --journal-allocator-aggressive-no-trade-scale 0.05
  --journal-allocator-protect-scale 0.45
  --journal-allocator-lock-scale 1.00
  --journal-allocator-max-risk-pct 0
  --journal-allocator-max-margin-pct 100
  --journal-regime-exit-profile
  --journal-defensive-exit-scope short_reversion
  --journal-defensive-max-holding-bars 24
  --journal-defensive-exit-weights 0.25,0.35,0.40
  --nim-candidate-review
  --nim-query-policy auto
  --nim-cache-only
  --nim-cache-path testnet/data/nim_review_cache_local.json
  --nim-timeout 5
  --minimax-fallback
  --minimax-timeout 120
  --ai-risk-judge
  --ai-risk-query-policy auto
  --ai-risk-cache-path testnet/data/ai_risk_judge_cache_auto_v3_prompt.json
  --interval 5m
  --compounding
  --daily-target-min-pct 3
  --daily-target-max-pct 3
  --risk 100
  --min-score 60
  --max-leverage 70
  --daily-soft-loss-pct 16
  --daily-max-loss-pct 36
  --daily-loss-risk-scale 0.55
  --daily-target-stop-pct 10
  --max-position-margin-pct 100
  --cooldown-bars 4
  --loss-cooldown-after 3
  --loss-cooldown-bars 18
  --max-holding-bars 48
  --journal-rolling-loss-lookback-days 1
  --journal-rolling-loss-pause-pct 6
  --journal-throttle-enabled
  --journal-throttle-strategy-scope long
  --journal-throttle-max-losses 1
  --journal-throttle-loss-pct 4
  --journal-throttle-risk-scale 0
  --take-profit-r 0.55,1.1,2.2
  --exit-weights 0.25,0.35,0.40
  --orb-session-start-bar 0
  --orb-opening-range-bars 9
  --orb-min-volume-ratio 0.8
  --orb-stop-atr 0.6
)

cd "${ROOT_DIR}"
mkdir -p testnet/results/segmented_backtests

echo "[LONG] python=${PYTHON_BIN}"
echo "[LONG] dates=${START_DATE}..${END_DATE}"
echo "[LONG] label=${LABEL}"
echo "[LONG] timeout=${PER_SEGMENT_TIMEOUT}s"

"${PYTHON_BIN}" scripts/segmented_backtest.py \
  --start-date "${START_DATE}" \
  --end-date "${END_DATE}" \
  --calendar-months \
  --continue-on-error \
  --per-segment-timeout "${PER_SEGMENT_TIMEOUT}" \
  --output-dir "testnet/results/segmented_backtests/${LABEL}_calendar" \
  -- \
  "${COMMON_ARGS[@]}"

"${PYTHON_BIN}" scripts/segmented_backtest.py \
  --start-date "${START_DATE}" \
  --end-date "${END_DATE}" \
  --window-days 30 \
  --step-days 15 \
  --continue-on-error \
  --per-segment-timeout "${PER_SEGMENT_TIMEOUT}" \
  --output-dir "testnet/results/segmented_backtests/${LABEL}_rolling_w30_s15" \
  -- \
  "${COMMON_ARGS[@]}"

"${PYTHON_BIN}" scripts/segmented_backtest.py \
  --start-date "${START_DATE}" \
  --end-date "${END_DATE}" \
  --window-days 14 \
  --step-days 7 \
  --continue-on-error \
  --per-segment-timeout "${PER_SEGMENT_TIMEOUT}" \
  --output-dir "testnet/results/segmented_backtests/${LABEL}_rolling_w14_s7" \
  -- \
  "${COMMON_ARGS[@]}"
