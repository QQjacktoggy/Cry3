#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Cry3 Deploy Wrapper (run locally)
# 用途：本地推送代碼、觸發 VM 部署腳本
# ============================================================

VM_NAME="cry3jack"
VM_ZONE="asia-east1-a"
REPO_DIR="/home/jack_shih/cry3"

echo "==> [local] Push to origin should be done BEFORE running deploy"
echo "==> [vm] Deploying to ${VM_NAME} (${VM_ZONE})"

# Copy deploy script to VM and run it
gcloud compute ssh "${VM_NAME}" --zone="${VM_ZONE}" \
  --command="cd ~/cry3 && git fetch origin && git reset --hard origin/main && bash scripts/deploy_vm.sh"

echo "==> [local] Deploy finished!"
