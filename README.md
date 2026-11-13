# Gold Ten Paper Bot

Paper-trades the course's MCX Gold Ten breakout strategy on live prices, with a
mobile dashboard. **It never places real orders.**

```
goldten-paper-bot/
├── bot/            Tier 2 worker: the strategy and the live loop
│   ├── strategy.py   rules engine + paper wallet (pure Python, fully tested)
│   ├── feed.py       Angel One prices (WebSocket + polling backup) and a simulator
│   ├── store.py      SQLite (Tier 3)
│   ├── notify.py     Telegram + Google Sheets
│   └── main.py       the always-on loop
├── api/main.py     Tier 2 read-only FastAPI for the dashboard
├── web/            Tier 1 dashboard (HTML/CSS/JS)
├── deploy/         systemd + Nginx files for the server
└── tests/          one test per strategy rule
```

## Phase 1: run it on your laptop

```bash
# 1. get the code and make a virtual environment
cd goldten-paper-bot
python -m venv .venv
.venv\Scripts\activate          # Windows   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# 2. check every rule
python -m pytest -q             # expect: 8 passed

# 3. settings
copy .env.example .env          # Mac/Linux: cp .env.example .env

# 4. terminal 1: the bot on a FAKE market (no account needed, fast clock)
python -m bot.main --sim --speed 600

# 5. terminal 2: the dashboard
uvicorn api.main:app --port 8000
#    open http://localhost:8000
```

When the Angel One keys are in `.env`, drop `--sim` to watch the real Gold Ten
market. Delete the `data/` folder to reset the paper wallet to ₹60,000.

## Rules implemented
Entry 4DHH/4DLL ± 0.12%, 2 lots, target 1.5% on 1 lot, SL1 (2DLL/2DHH) and
SL2 (4DLL/4DHH) trailing daily, gap rules from the 09:00–09:15 candle, rollover
at 9 PM when fewer than 10 trading days remain (same target/SL numbers),
₹60,000 paper wallet with ₹30,000 margin per lot and MCX charges.
