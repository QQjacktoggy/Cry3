#!/usr/bin/env bash
set -e
set -u

VM_NAME="cry3jack"
VM_ZONE="asia-east1-a"
REPO_DIR="~/cry3"
BRANCH="main"

echo "==> Pushing local commits (if any) to origin/$BRANCH"
git push origin "$BRANCH"

echo "==> Deploying to $VM_NAME ($VM_ZONE)"

gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --command="bash -lc '
set -euo pipefail
cd $REPO_DIR

echo \"--- git pull ---\"
git fetch origin
git reset --hard origin/$BRANCH
git log -1 --oneline

echo \"--- install deps (if pyproject changed) ---\"
if [ -d .venv ]; then
  source .venv/bin/activate
fi
pip install -e . --quiet || pip install -r pyproject.toml --quiet || true

echo \"--- detect & restart bot ---\"
RESTARTED=0

for svc in cry3 gridbot cry3-bot binance-grid; do
  if systemctl list-unit-files | grep -q \"^\${svc}.service\"; then
    echo \"[systemd] restarting \$svc\"
    sudo systemctl restart \"\$svc\"
    sudo systemctl status \"\$svc\" --no-pager -l | head -20
    RESTARTED=1
    break
  fi
done

if [ \$RESTARTED -eq 0 ] && [ -f docker-compose.yml ]; then
  echo \"[docker] compose up -d --build\"
  docker compose up -d --build
  docker compose ps
  RESTARTED=1
fi

if [ \$RESTARTED -eq 0 ] && command -v tmux >/dev/null && tmux has-session -t cry3 2>/dev/null; then
  echo \"[tmux] restarting cry3 session\"
  tmux kill-session -t cry3
  tmux new-session -d -s cry3 \"cd $REPO_DIR && source .venv/bin/activate 2>/dev/null; python main.py\"
  RESTARTED=1
fi

if [ \$RESTARTED -eq 0 ]; then
  echo \"[nohup] killing old python main.py and respawning\"
  pkill -f \"python.*main.py\" || true
  sleep 1
  nohup bash -c \"cd $REPO_DIR && source .venv/bin/activate 2>/dev/null; python main.py\" > ~/cry3.log 2>&1 &
  sleep 2
  ps aux | grep -E \"python.*main.py\" | grep -v grep || echo \"WARN: bot not visible in ps\"
fi

echo \"--- done ---\"
'"

echo "==> Deploy finished. Tail log via:"
echo "    gcloud compute ssh $VM_NAME --zone=$VM_ZONE --command='tail -f ~/cry3.log'"
