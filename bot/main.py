"""Gold Ten paper-trading bot — the long-running worker (Tier 2).

Run:   python -m bot.main            (real Angel One feed, settings from .env)
       python -m bot.main --sim      (fake market with a fast clock, no account needed)
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import queue
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .feed import AngelFeed, AuthError, SimFeed
from .notify import Sheets, Telegram
from .store import Store
from .strategy import Engine, Wallet

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
log = logging.getLogger("bot")

ALERT_KINDS = {"ENTRY", "TARGET", "STOP", "ROLL_OUT", "ROLL_IN", "GAP", "SKIP"}
SIDE = {1: "BUY", -1: "SELL", 0: ""}


def env(name, default=None, cast=str):
    v = os.getenv(name, default)
    return cast(v) if v not in (None, "") else v


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_dir / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def session_close(day: dt.date) -> dt.time:
    """MCX bullion closes 23:30 IST, or 23:55 while the US is on daylight-saving time."""
    ny = dt.datetime.combine(day, dt.time(12, 0), tzinfo=ZoneInfo("America/New_York"))
    return dt.time(23, 55) if ny.dst() else dt.time(23, 30)


class Bot:
    def __init__(self, sim: bool, speed: float):
        self.store = Store(ROOT / env("DB_PATH", "data/goldten.db"))
        self.tg = Telegram(env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID"))
        self.sheets = Sheets(env("GOOGLE_CREDS_FILE"), env("GOOGLE_SHEET_ID"), env("GOOGLE_SHEET_TAB", "Gold Ten Trades"))
        self.holidays = frozenset(dt.date.fromisoformat(d.strip())
                                  for d in (env("MCX_HOLIDAYS", "") or "").split(",") if d.strip())
        if sim:
            self.feed = SimFeed(speed=speed)
            self.mode = "sim"
        else:
            self.feed = AngelFeed(env("ANGEL_API_KEY"), env("ANGEL_CLIENT_CODE"), env("ANGEL_PIN"),
                                  env("ANGEL_TOTP_SECRET"))
            self.mode = "live"
        wallet = Wallet(env("START_CAPITAL", 60000, float),
                        margin_pct=env("MARGIN_PCT", 10, float) / 100,
                        buffer=env("MARGIN_BUFFER_PCT", 25, float) / 100)
        self.engine = Engine(wallet, lots=env("LOTS", 2, int), roll_days=env("ROLL_DAYS", 10, int),
                             roll_time=dt.time.fromisoformat(env("ROLL_TIME", "21:00")),
                             holidays=self.holidays)
        self.engine.load_state(self.store.get("engine_state"))
        self.ticks: queue.Queue = queue.Queue()
        self.prepared_day: dt.date | None = None
        self.session_state = "starting"
        self.token_to_symbol: dict[str, str] = {}
        self.last_beat = self.last_equity = 0.0
        self.tick_count = 0

    # ------------------------------------------------------------------ events
    def handle(self, events):
        if not events:
            return
        snap = self.engine.snapshot()
        for e in events:
            self.store.add_event(e)
            msg = f"{e.kind:<8} {e.contract} {SIDE[e.side]} {e.lots or ''} @ {e.price or ''} | {e.note}"
            log.info(msg)
            self.sheets.trade_event(e, snap.get("position"), snap.get("levels"))
            if e.kind in ALERT_KINDS:
                pnl = f"\nP/L: ₹{e.pnl:,.0f}" if e.pnl else ""
                self.tg.send(f"Gold Ten paper bot\n{e.kind} {SIDE[e.side]} {e.contract}\n"
                             f"Price: {e.price:,.0f}{pnl}\n{e.note}")
        self.store.set("engine_state", self.engine.save_state())

    # ------------------------------------------------------------------ morning
    def prepare(self, day: dt.date, now: dt.datetime):
        self.session_state = "preparing"
        self.beat(force=True)
        saved_intraday = (self.store.get("engine_state") or {}).get("intraday")   # read before anything saves
        self.feed.login()
        contracts = self.feed.contracts()
        self.token_to_symbol = {c["token"]: s for s, c in contracts.items()}
        expiries = {s: c["expiry"] for s, c in contracts.items()}
        from .strategy import pick_contract
        active = pick_contract(day, expiries, self.engine.roll_days, self.holidays)
        needed = {active} | ({self.engine.pos.contract} if self.engine.pos else set())
        needed.discard(None)
        bars = {s: self.feed.daily_bars(contracts[s]["token"], day) for s in needed}
        self.handle(self.engine.prepare_day(day, expiries, bars))
        if self.engine.pos:     # daily stop-loss trail -> tracking sheet
            self.sheets.update_sl(self.engine.pos.opened, round(self.engine.pos.sl or 0, 2))

        # Started late or restarted mid-session? Pick up where we left off, then replay what we missed
        # (1-minute candles) through the same rules, so gaps, entries and stops match a bot that ran all day.
        resume = self.engine.restore_intraday(saved_intraday)
        if now.time() > dt.time(9, 0):
            start = dt.datetime.fromisoformat(resume) + dt.timedelta(seconds=1) if resume \
                else dt.datetime.combine(day, dt.time(9, 0))
            self.replay(start, now, {s: contracts[s]["token"] for s in needed})
        tokens = [contracts[s]["token"] for s in needed]
        self.feed.stop_stream()
        self.feed.start_stream(tokens, lambda tok, px: self.ticks.put((tok, px)))
        self.prepared_day = day
        self.session_state = "open"
        self.store.set("contracts", {s: {"token": c["token"], "expiry": c["expiry"].isoformat()}
                                     for s, c in contracts.items()})

    def replay(self, start: dt.datetime, end: dt.datetime, tokens: dict):
        from .feed import candles_to_ticks
        merged = []
        for sym, tok in tokens.items():
            try:
                merged += [(t, sym, px) for t, px in candles_to_ticks(self.feed.minute_candles(tok, start, end))]
            except Exception as exc:
                log.warning("Replay: no 1-minute candles for %s (%s)", sym, exc)
        merged.sort(key=lambda x: x[0])
        if not merged:
            return
        log.info("Replaying %d prices from %s to %s", len(merged), start.strftime("%H:%M"), end.strftime("%H:%M"))
        for t, sym, px in merged:
            evs = self.engine.on_tick(t, sym, px)
            for e in evs:
                e.note += " [replayed]"
            self.handle(evs)
        self.store.set("engine_state", self.engine.save_state())

    # ------------------------------------------------------------------ status
    def beat(self, force=False):
        if not force and time.time() - self.last_beat < 3:
            return
        self.last_beat = time.time()
        stream_age = time.time() - getattr(self.feed, "last_stream_tick", 0)
        self.store.set("heartbeat", {
            "market_time": self.feed.now().isoformat(timespec="seconds"),
            "session": self.session_state, "mode": self.mode,
            "feed": "stream" if stream_age < 15 else "polling",
            "stream_age_s": round(stream_age, 1), "ticks": self.tick_count,
        })
        snap = self.engine.snapshot()
        self.store.set("snapshot", snap)
        if self.session_state == "open":   # remember how far today has been processed (restart resumes here)
            self.store.set("engine_state", self.engine.save_state())
        if time.time() - self.last_equity >= 60 and self.session_state == "open":
            self.last_equity = time.time()
            w = snap["wallet"]
            self.store.add_equity(self.feed.now().isoformat(timespec="minutes"), w["equity"], w["realized"],
                                  snap["unrealized"], w["charges"])

    # ------------------------------------------------------------------ loop
    def run(self):
        log.info("Bot starting in %s mode", self.mode)
        self.tg.send(f"Gold Ten paper bot started ({self.mode})")
        while True:
            try:
                self.loop_once()
            except KeyboardInterrupt:
                raise
            except AuthError as exc:
                # Wrong PIN / TOTP / API key: do NOT keep retrying (Angel can lock the account).
                log.error("%s. Fix .env, then: sudo systemctl restart goldten-bot", exc)
                self.tg.send(f"⛔ Gold Ten bot login rejected: {exc}. Fix .env and restart the bot.")
                self.session_state = "login_failed"
                while True:                   # stay up so the dashboard shows the problem
                    self.beat(force=True)
                    time.sleep(10)
            except Exception as exc:
                log.exception("Loop error: %s", exc)
                self.tg.send(f"⚠️ Gold Ten bot error: {exc}")
                self.session_state = "error"
                self.beat(force=True)
                self.prepared_day = None      # re-login and re-prepare on the next pass
                time.sleep(30)

    def loop_once(self):
        now = self.feed.now()
        day = now.date()
        close = session_close(day)
        trading_day = day.weekday() < 5 and day not in self.holidays
        if not trading_day or now.time() < dt.time(8, 45) or now.time() >= close:
            if self.session_state == "open":
                self.feed.stop_stream()
                self.store.set("engine_state", self.engine.save_state())
                log.info("Session closed for %s. Equity %.0f", day, self.engine.snapshot()["wallet"]["equity"])
            self.session_state = "closed" if trading_day else "holiday"
            self.beat()
            time.sleep(1)
            return
        if self.prepared_day != day:
            self.prepare(day, now)

        # drain live prices (one thread handles all strategy logic -> no race conditions)
        got = False
        try:
            for i in range(500):          # a batch, then fall through to the heartbeat
                tok, px = self.ticks.get(timeout=0.5) if i == 0 else self.ticks.get_nowait()
                sym = self.token_to_symbol.get(tok)
                if sym and px > 0:
                    got = True
                    self.tick_count += 1
                    self.handle(self.engine.on_tick(self.feed.now(), sym, px))
        except queue.Empty:
            pass

        # backup: stream silent for 15 s -> poll prices over REST
        if not got and time.time() - getattr(self.feed, "last_stream_tick", 0) > 15:
            for tok, sym in self.token_to_symbol.items():
                if sym == self.engine.active or (self.engine.pos and sym == self.engine.pos.contract):
                    px = self.feed.ltp(sym, tok)
                    self.tick_count += 1
                    self.handle(self.engine.on_tick(self.feed.now(), sym, px))
            time.sleep(3)
        self.beat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="use the fake market (no Angel One account needed)")
    ap.add_argument("--speed", type=float, default=600, help="sim only: market seconds per real second")
    args = ap.parse_args()
    setup_logging(ROOT / env("LOG_DIR", "logs"))
    Bot(args.sim, args.speed).run()


if __name__ == "__main__":
    main()
