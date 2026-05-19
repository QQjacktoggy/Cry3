#!/usr/bin/env bash
gcloud compute ssh cry3jack --zone=asia-east1-a --command="bash -lc '
echo \"=== systemd services ===\"
systemctl list-units --type=service --state=running | grep -iE \"cry|grid|bot\" || echo none
echo
echo \"=== docker ===\"
docker ps 2>/dev/null || echo \"docker not installed\"
echo
echo \"=== tmux sessions ===\"
tmux ls 2>/dev/null || echo none
echo
echo \"=== python processes ===\"
ps aux | grep -E \"python.*main.py\" | grep -v grep || echo none
echo
echo \"=== repo state ===\"
cd ~/cry3 2>/dev/null && git log -1 --oneline && git status -s || echo \"~/cry3 not found\"
'"
