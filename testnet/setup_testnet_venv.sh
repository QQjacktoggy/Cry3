#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install \
  "python-binance>=1.0.20" \
  "google-genai>=1.0.0" \
  "python-telegram-bot>=21.0" \
  "aiosqlite>=0.20.0" \
  "pydantic>=2.7.0" \
  "pydantic-settings>=2.3.0" \
  "APScheduler>=3.10.4,<4.0" \
  "structlog>=24.1.0" \
  "python-dotenv>=1.0.1"

echo "OK: testnet venv ready at ${VENV_DIR}"
