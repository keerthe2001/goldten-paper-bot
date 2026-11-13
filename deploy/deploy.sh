#!/usr/bin/env bash
# Pull the latest code from GitHub and restart the bot safely.
# Run on the server:  ~/goldten-paper-bot/deploy/deploy.sh
set -euo pipefail                      # stop at the first error

cd "$(dirname "$0")/.."
echo "==> Fetching latest code"
git pull --ff-only                     # refuses if the server copy was edited by hand

echo "==> Installing any new Python packages"
.venv/bin/pip install -q -r requirements.txt

echo "==> Running the rule tests"
.venv/bin/python -m pytest -q         # a failing test stops the deploy: the running bot is untouched

echo "==> Restarting services"
sudo systemctl restart goldten-bot goldten-api
sleep 3
systemctl is-active goldten-bot goldten-api
echo "==> Deployed $(git log -1 --format='%h %s')"
