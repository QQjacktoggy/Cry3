#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.testnet"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}."
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

echo "BINANCE_TESTNET=${BINANCE_TESTNET:-}"
echo "TRADING_SYMBOLS=${TRADING_SYMBOLS:-}"
echo "DB_PATH=${DB_PATH:-}"
echo "TRADING_MODE=${TRADING_MODE:-signal_only}"
echo "MAX_EFFECTIVE_LEVERAGE=${MAX_EFFECTIVE_LEVERAGE:-20}"
echo "MAX_DAILY_LOSS_PCT=${MAX_DAILY_LOSS_PCT:-1.5}"
echo "MAX_TRADE_RISK_PCT=${MAX_TRADE_RISK_PCT:-0.5}"

if [[ "${BINANCE_TESTNET:-}" != "true" ]]; then
  echo "FAIL: BINANCE_TESTNET must be true."
  exit 1
fi

case "${DB_PATH:-}" in
  testnet/*) echo "OK: DB_PATH is isolated." ;;
  *)
    echo "FAIL: DB_PATH must stay under testnet/."
    exit 1
    ;;
esac

if [[ -z "${BINANCE_API_KEY:-}" || -z "${BINANCE_API_SECRET:-}" ]]; then
  echo "WARN: Binance testnet API key/secret are empty."
else
  echo "OK: Binance testnet key/secret are present."
fi
