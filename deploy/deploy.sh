#!/usr/bin/env bash
# Pull the latest code from GitHub and restart only what changed.
# Run on the server:  ~/goldten-paper-bot/deploy/deploy.sh
set -euo pipefail                      # stop at the first error

cd "$(dirname "$0")/.."
before=$(git rev-parse HEAD)
echo "==> Fetching latest code"
git pull --ff-only                     # refuses if the server copy was edited by hand
changed=$(git diff --name-only "$before" HEAD)
if [ -z "$changed" ]; then
  echo "==> Already up to date, nothing restarted"
  exit 0
fi
echo "$changed" | sed 's/^/    changed: /'

if echo "$changed" | grep -q '^requirements.txt$'; then
  echo "==> Installing new Python packages"
  .venv/bin/pip install -q -r requirements.txt
fi

echo "==> Running the rule tests"
.venv/bin/python -m pytest -q         # a failing test stops the deploy: the running bot is untouched

# The bot restarts only when its own code changed (it picks up where it left off, but there is
# no reason to interrupt it for a dashboard tweak). Dashboard files (web/) need no restart at all.
if echo "$changed" | grep -qE '^(bot/|requirements.txt)'; then
  echo "==> Restarting the bot"
  sudo systemctl restart goldten-bot
fi
if echo "$changed" | grep -qE '^(api/|bot/store.py|requirements.txt)'; then
  echo "==> Restarting the dashboard API"
  sudo systemctl restart goldten-api
fi
sleep 2
systemctl is-active goldten-bot goldten-api
echo "==> Deployed $(git log -1 --format='%h %s')"
echo "    (changed .env? run: sudo systemctl restart goldten-bot goldten-api)"
