#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.testnet"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy testnet/.env.testnet.example to testnet/.env.testnet first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ "${BINANCE_TESTNET:-}" != "true" ]]; then
  echo "Refusing to start: BINANCE_TESTNET must be true." >&2
  exit 1
fi

case "${DB_PATH:-}" in
  testnet/*) ;;
  *)
    echo "Refusing to start: DB_PATH must stay under testnet/." >&2
    exit 1
    ;;
esac

mkdir -p "${ROOT_DIR}/testnet/data" "${ROOT_DIR}/testnet/logs"

PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing ${PYTHON_BIN}. Create it with: python3 -m venv testnet/.venv && testnet/.venv/bin/python -m pip install -e ." >&2
  exit 1
fi

echo "[TESTNET] root=${ROOT_DIR}"
echo "[TESTNET] symbols=${TRADING_SYMBOLS:-}"
echo "[TESTNET] db=${DB_PATH:-}"
echo "[TESTNET] mode=${TRADING_MODE:-signal_only}"
echo "[TESTNET] binance_testnet=${BINANCE_TESTNET:-}"
echo "[TESTNET] python=${PYTHON_BIN}"
echo "[TESTNET] strategy=${TESTNET_STRATEGY_LABEL:-router_allocator_v13_trend350}"
echo "[TESTNET] daily_target_pct=${TESTNET_DAILY_TARGET_PCT:-2.7}"
echo "[TESTNET] max_effective_leverage=${MAX_EFFECTIVE_LEVERAGE:-70}"
echo "[TESTNET] daily_soft_loss_pct=${DAILY_SOFT_LOSS_PCT:-16}"
echo "[TESTNET] max_daily_loss_pct=${MAX_DAILY_LOSS_PCT:-36}"

cd "${ROOT_DIR}"
exec "${PYTHON_BIN}" main.py
