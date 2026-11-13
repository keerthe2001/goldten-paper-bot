"""Market data feeds.

AngelFeed  - real MCX GOLDTEN prices from Angel One SmartAPI
             (WebSocket stream first, REST polling as automatic backup).
SimFeed    - a fake Gold Ten market with a fast clock, for testing on your laptop.

Both expose the same methods, so main.py does not care which one it uses.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import random
import threading
import time
from typing import Callable

import requests

from .strategy import Bar

log = logging.getLogger("feed")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


def _retry(fn, what, tries=5, base=2.0):
    """Call fn(); on failure wait 2, 4, 8, 16 s ... (exponential backoff)."""
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            wait = base * 2 ** i
            log.warning("%s failed (%s); retry %d/%d in %.0fs", what, exc, i + 1, tries, wait)
            time.sleep(wait)
    raise RuntimeError(f"{what} failed after {tries} tries")


class AngelFeed:
    def __init__(self, api_key, client_code, pin, totp_secret, symbol="GOLDTEN"):
        self.api_key, self.client_code, self.pin, self.totp_secret = api_key, client_code, pin, totp_secret
        self.symbol = symbol
        self.api = None
        self.jwt = self.feed_token = None
        self.sws = None
        self._stream_cb: Callable | None = None
        self.last_stream_tick = 0.0

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(IST).replace(tzinfo=None)

    def sleep(self, seconds):
        time.sleep(seconds)

    # ---- login ----
    def login(self):
        import pyotp
        from SmartApi import SmartConnect

        def _do():
            api = SmartConnect(api_key=self.api_key)
            res = api.generateSession(self.client_code, self.pin, pyotp.TOTP(self.totp_secret).now())
            if not res or not res.get("status"):
                raise RuntimeError(f"login rejected: {res.get('message') if res else res}")
            self.api = api
            self.jwt = res["data"]["jwtToken"].replace("Bearer ", "")
            self.feed_token = api.getfeedToken()
        _retry(_do, "Angel One login")
        log.info("Logged in to Angel One as %s", self.client_code)

    # ---- instruments ----
    def contracts(self) -> dict:
        """{trading_symbol: {"token": str, "expiry": date}} for GOLDTEN futures."""
        data = _retry(lambda: requests.get(SCRIP_MASTER, timeout=60).json(), "Scrip master download")
        out = {}
        for r in data:
            if r.get("exch_seg") == "MCX" and r.get("name") == self.symbol and r.get("instrumenttype") == "FUTCOM":
                exp = dt.datetime.strptime(r["expiry"], "%d%b%Y").date()
                out[r["symbol"]] = {"token": r["token"], "expiry": exp}
        if not out:
            raise RuntimeError(f"No {self.symbol} futures found in scrip master")
        return out

    # ---- history ----
    def _candles(self, token, interval, start: dt.datetime, end: dt.datetime):
        params = {"exchange": "MCX", "symboltoken": token, "interval": interval,
                  "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")}

        def _do():
            res = self.api.getCandleData(params)
            if not res or not res.get("status"):
                raise RuntimeError(res.get("message") if res else "empty response")
            return res.get("data") or []
        rows = _retry(_do, f"Candles {interval} {token}")
        time.sleep(0.5)  # stay well under the 3 requests/second candle limit
        return rows

    def daily_bars(self, token, before: dt.date, days=20) -> list[Bar]:
        start = dt.datetime.combine(before - dt.timedelta(days=days), dt.time(9, 0))
        end = dt.datetime.combine(before - dt.timedelta(days=1), dt.time(23, 59))
        bars = []
        for ts, o, h, l, c, *_ in self._candles(token, "ONE_DAY", start, end):
            d = dt.datetime.fromisoformat(ts).date()
            if d < before:
                bars.append(Bar(d, float(o), float(h), float(l), float(c)))
        return bars

    def first15(self, token, day: dt.date):
        """High/low of the 09:00-09:15 candle, or None if not available yet."""
        rows = self._candles(token, "FIFTEEN_MINUTE", dt.datetime.combine(day, dt.time(9, 0)),
                             dt.datetime.combine(day, dt.time(9, 15)))
        for ts, o, h, l, c, *_ in rows:
            if dt.datetime.fromisoformat(ts).time() == dt.time(9, 0):
                return float(h), float(l), float(o)
        return None

    # ---- live prices ----
    def ltp(self, trading_symbol, token) -> float:
        def _do():
            res = self.api.ltpData("MCX", trading_symbol, token)
            if not res or not res.get("status"):
                raise RuntimeError(res.get("message") if res else "empty response")
            return float(res["data"]["ltp"])
        return _retry(_do, f"LTP {trading_symbol}", tries=3, base=1.0)

    def start_stream(self, tokens: list[str], on_price: Callable[[str, float], None]):
        """WebSocket stream. Prices arrive in paise -> divide by 100."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        self._stream_cb = on_price
        sws = SmartWebSocketV2(self.jwt, self.api_key, self.client_code, self.feed_token,
                               max_retry_attempt=5, retry_strategy=1, retry_delay=5)

        def on_data(wsapp, msg):
            try:
                self.last_stream_tick = time.time()
                on_price(str(msg["token"]), msg["last_traded_price"] / 100.0)
            except Exception as exc:
                log.warning("Bad stream message: %s", exc)

        sws.on_open = lambda wsapp: sws.subscribe("goldten", 1, [{"exchangeType": 5, "tokens": tokens}])
        sws.on_data = on_data
        sws.on_error = lambda *a: log.warning("Stream error: %s", a[-1] if a else "")
        sws.on_close = lambda wsapp: log.warning("Stream closed")
        self.sws = sws
        threading.Thread(target=sws.connect, daemon=True, name="angel-ws").start()
        log.info("WebSocket stream started for tokens %s", tokens)

    def stop_stream(self):
        if self.sws:
            try:
                self.sws.close_connection()
            except Exception:
                pass
            self.sws = None


class SimFeed:
    """A pretend Gold Ten market for local testing.
    `speed` = how many market seconds pass per real second (600 -> a 14.5 h session in ~90 s)."""

    def __init__(self, start_day: dt.date | None = None, speed: float = 600, seed: int = 7,
                 start_price: float = 128000.0):
        self.speed = speed
        self.rng = random.Random(seed)
        self.t0_real = time.time()
        day = start_day or dt.date.today()
        while day.weekday() >= 5:          # start the fake market on a weekday
            day -= dt.timedelta(days=1)
        self.t0_mkt = dt.datetime.combine(day, dt.time(8, 50))
        self.price = start_price
        self.history: dict[str, list[Bar]] = {}
        self.last_stream_tick = time.time()
        self._cb = None
        self._contracts = self._make_contracts(day)
        self._seed_history(day)

    def now(self) -> dt.datetime:
        return self.t0_mkt + dt.timedelta(seconds=(time.time() - self.t0_real) * self.speed)

    def sleep(self, seconds):
        time.sleep(seconds / self.speed)

    def login(self):
        log.info("SimFeed: pretend login OK")

    def _make_contracts(self, day):
        out, d = {}, day.replace(day=1)
        for i in range(4):
            m = (d.month - 1 + i) % 12 + 1
            y = d.year + (d.month - 1 + i) // 12
            nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
            exp = nxt - dt.timedelta(days=1)
            while exp.weekday() >= 5:
                exp -= dt.timedelta(days=1)
            out[f"GOLDTEN{exp:%y%b}FUT".upper()] = {"token": f"SIM{i}", "expiry": exp}
        return out

    def contracts(self):
        return self._contracts

    def _seed_history(self, day):
        px = self.price
        bars, d = [], day - dt.timedelta(days=30)
        while d < day:
            if d.weekday() < 5:
                o = px
                moves = [self.rng.gauss(0, 0.004) for _ in range(20)]
                path = [o]
                for m in moves:
                    path.append(path[-1] * (1 + m / math.sqrt(20) * 2))
                bars.append(Bar(d, o, max(path), min(path), path[-1]))
                px = path[-1]
            d += dt.timedelta(days=1)
        self.price = px
        for i, sym in enumerate(sorted(self._contracts, key=lambda s: self._contracts[s]["expiry"])):
            prem = 1 + 0.006 * i      # later contracts trade at a small premium
            self.history[self._contracts[sym]["token"]] = [Bar(b.date, b.open * prem, b.high * prem,
                                                               b.low * prem, b.close * prem) for b in bars]

    def daily_bars(self, token, before, days=20):
        return [b for b in self.history.get(token, []) if b.date < before][-days:]

    def first15(self, token, day):
        return None   # SimFeed builds the 09:15 candle from its own ticks

    def ltp(self, trading_symbol, token):
        i = int(token[3:])
        return round(self.price * (1 + 0.006 * i), 0)

    def step(self):
        """Advance the fake market by one tick (called by the sim stream thread)."""
        drift = 0.00002 * math.sin(time.time() / 50)
        self.price *= 1 + drift + self.rng.gauss(0, 0.0006)

    def start_stream(self, tokens, on_price):
        self._cb = on_price

        def run():
            while self._cb:
                self.step()
                self.last_stream_tick = time.time()
                for sym, c in self._contracts.items():
                    if c["token"] in tokens:
                        on_price(c["token"], self.ltp(sym, c["token"]))
                time.sleep(0.05)
        threading.Thread(target=run, daemon=True, name="sim-stream").start()

    def stop_stream(self):
        self._cb = None
