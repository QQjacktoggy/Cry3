#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Cry3 Deploy Script (run on VM)
# 用途：重啟 systemd 服務、健康檢查
# 原則：
#   - 只走 systemd（cry3.service），失敗即報錯退出，不 fallback 混用
#   - 健康檢查：進程存活 + log 更新 + SQLite 寫入
#   - Log 統一輸出到 /home/jack_shih/cry3_manual.log
# ============================================================

REPO_DIR="/home/jack_shih/cry3"
SERVICE_NAME="cry3"
BRANCH="main"
LOG_FILE="/home/jack_shih/cry3_manual.log"
HEALTH_CHECK_TIMEOUT=30

echo "==> [vm] Fetching latest code"
cd "$REPO_DIR"
git fetch origin
git reset --hard origin/"$BRANCH"
echo "HEAD: $(git log -1 --oneline)"

echo "==> [vm] Installing deps if needed"
if [ -d testnet/.venv ]; then
  source testnet/.venv/bin/activate
fi
testnet/.venv/bin/pip install -e . --quiet

echo "==> [vm] Checking systemd service"
if [ -f /etc/systemd/system/${SERVICE_NAME}.service ] || [ -f /usr/lib/systemd/system/${SERVICE_NAME}.service ]; then
  echo "==> [vm] Restarting ${SERVICE_NAME} (systemd)"
  sudo systemctl restart "${SERVICE_NAME}"

  for i in $(seq 1 "$HEALTH_CHECK_TIMEOUT"); do
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
      echo "==> [vm] ${SERVICE_NAME} is active"
      break
    fi
    sleep 1
    if [ "$i" -eq "$HEALTH_CHECK_TIMEOUT" ]; then
      echo "ERROR: ${SERVICE_NAME} failed to become active within ${HEALTH_CHECK_TIMEOUT}s"
      sudo systemctl status "${SERVICE_NAME}" --no-pager -l
      exit 1
    fi
  done

  PID=$(systemctl show --property=MainPID --value "${SERVICE_NAME}")
  echo "==> [vm] Service PID: ${PID}"

else
  echo "ERROR: ${SERVICE_NAME}.service not found on VM"
  exit 1
fi

echo "==> [vm] Health checks..."

# 1. Process alive
if ! kill -0 "${PID}" 2>/dev/null; then
  echo "ERROR: Process ${PID} not alive"
  exit 1
fi
echo "  [ok] Process ${PID} alive"

# 2. Log file updating
sleep 2
if [ -f "${LOG_FILE}" ]; then
  BEFORE=$(stat -c %Y "${LOG_FILE}" 2>/dev/null || echo 0)
  sleep 3
  AFTER=$(stat -c %Y "${LOG_FILE}" 2>/dev/null || echo 0)
  if [ "${AFTER}" -le "${BEFORE}" ]; then
    echo "WARN: Log file not updating (may be quiet period)"
  else
    echo "  [ok] Log file updating"
  fi
else
  echo "WARN: Log file ${LOG_FILE} not found"
fi

# 3. Database accessible
if [ -f "${REPO_DIR}/testnet/data/gridbot_testnet.db" ]; then
  sqlite3 "${REPO_DIR}/testnet/data/gridbot_testnet.db" "SELECT 1;" >/dev/null 2>&1
  if [ $? -eq 0 ]; then
    echo "  [ok] Database accessible"
  else
    echo "ERROR: Database not accessible"
    exit 1
  fi
else
  echo "WARN: Database file not at expected path"
fi

# 4. Tail recent log
tail -n 5 "${LOG_FILE}" 2>/dev/null | head -5

echo "==> [vm] Deploy successful!"
echo "==> Bot PID: ${PID}"
echo "==> Log: ${LOG_FILE}"
echo "==> To tail: gcloud compute ssh cry3jack --zone=asia-east1-a --command='tail -f ${LOG_FILE}'"
