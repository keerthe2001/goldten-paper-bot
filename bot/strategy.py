"""
Gold Ten breakout strategy — paper-trading engine.

Pure logic only: no network, no database. The bot (main.py) feeds it
  1. prepare_day(...)   once each morning with completed daily candles
  2. on_tick(...)       for every live price
and the engine returns a list of Event objects (entries, targets, stops,
rollovers, gap notices) that main.py saves, logs and sends out.

Rules (from the course's Gold Ten calculator sheet):
  Buy entry      = 4DHH * (1 + 0.12%)
  Buy target     = entry * (1 + 1.5%)            -> book 1 of 2 lots
  Buy SL1        = max(entry*(1-1.5%), 2DLL*(1-0.12%))   before target, both lots
  Buy SL2        = max(entry*(1-1.5%), 4DLL*(1-0.12%))   after target, last lot
  Sell side is the mirror image. Stops are recalculated every morning and
  only ever move in the trade's favour.

Gap rules (course "Gap Up / Gap Down" sheets):
  If the market OPENS beyond an entry level, skip it at the open, wait for the
  first 15-minute candle (09:00-09:15) and use its high (buy) / low (sell)
  +/- 0.125% as the new entry. Gap trades use: target 1.25%, SL1
  max(entry*(1-1.25%), 2DLL*(1-0.125%)), SL2 max(entry, 2DLL*(1-0.125%)).
  If the market OPENS beyond an open position's stop, do not exit at the open:
  the stop becomes the first 15-minute low (long) / high (short) -/+ 0.12%.

Rollover (course "Rollover" sheet): trade the nearest contract with at least
ROLL_DAYS trading days to expiry. When the open position's contract falls
below that, at ROLL_TIME exit it at market, enter the same position in the
next contract at market, and keep the same target / stop-loss numbers.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Optional

NORMAL = dict(T=0.015, M=0.015, B=0.0012, sl2_breakeven=False, sl2_days=4)
GAP = dict(T=0.0125, M=0.0125, B=0.00125, sl2_breakeven=True, sl2_days=2)


@dataclass
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float


@dataclass
class Levels:
    contract: str
    hh4: float
    ll4: float
    hh2: float
    ll2: float

    @property
    def buy_entry(self):
        return self.hh4 * (1 + NORMAL["B"])

    @property
    def sell_entry(self):
        return self.ll4 * (1 - NORMAL["B"])


@dataclass
class Position:
    side: int                 # +1 long, -1 short
    contract: str
    entry: float              # fill price in the CURRENT contract (P/L basis)
    level_base: float         # original entry price; target / stops are built from it
    lots: int
    target: float
    sl: Optional[float]
    params: dict
    opened: str               # ISO timestamp
    gap_trade: bool = False
    realized: float = 0.0     # P/L booked on this trade so far


@dataclass
class Event:
    ts: str
    kind: str                 # ENTRY, TARGET, STOP, ROLL_OUT, ROLL_IN, GAP, INFO, SKIP
    contract: str = ""
    side: int = 0
    lots: int = 0
    price: float = 0.0
    pnl: float = 0.0
    charges: float = 0.0
    note: str = ""
    trade: str = ""           # id of the trade this event belongs to (its entry time)
    total: float = 0.0        # the trade's realized P/L so far (incl. rollovers)


def _round(x):
    return None if x is None else round(x, 2)


class Wallet:
    """Paper funds, like the Kite funds page."""

    """Margin is dynamic, like on a real account: at each entry the bot blocks
    margin_pct x contract value at the live price, plus a safety buffer
    (brokers and MCX can raise margins overnight in volatile markets)."""

    def __init__(self, start_cash: float, margin_pct: float = 0.10, buffer: float = 0.25,
                 lot_multiplier: float = 1.0):
        self.start_cash = start_cash
        self.margin_pct = margin_pct    # e.g. 0.10 = 10% of contract value (read it off Kite's order window)
        self.buffer = buffer            # 0.25 = keep 25% extra above the exchange margin
        self.mult = lot_multiplier      # Gold Ten: price is per 10 g, 1 lot = 10 g -> 1
        self.realized = 0.0
        self.charges = 0.0
        self.blocked = 0.0
        self.lot_margin = 0.0           # margin blocked per lot for the open position

    @property
    def available(self):
        return self.start_cash + self.realized - self.charges - self.blocked

    def margin_for(self, price: float, lots: int) -> float:
        return price * self.mult * lots * self.margin_pct * (1 + self.buffer)

    def can_block(self, lots, price):
        return self.available >= self.margin_for(price, lots)

    def block(self, lots, price):
        self.lot_margin = self.margin_for(price, 1)
        self.blocked += self.lot_margin * lots

    def release(self, lots):
        self.blocked = max(0.0, self.blocked - lots * self.lot_margin)
        if self.blocked < 1e-6:
            self.blocked, self.lot_margin = 0.0, 0.0

    @staticmethod
    def order_charges(value: float, is_sell: bool) -> float:
        """Approximate MCX commodity-futures charges for ONE order of `value` rupees
        on a Zerodha-style account: brokerage min(Rs20, 0.03%), CTT 0.01% on sells,
        exchange fee ~0.0021%, SEBI fee Rs10/crore, GST 18% on (brokerage + fees),
        stamp duty 0.002% on buys."""
        brokerage = min(20.0, value * 0.0003)
        ctt = value * 0.0001 if is_sell else 0.0
        exch = value * 0.000021
        sebi = value * 10 / 1e7
        gst = 0.18 * (brokerage + exch + sebi)
        stamp = 0.0 if is_sell else value * 0.00002
        return brokerage + ctt + exch + sebi + gst + stamp

    def to_dict(self):
        return dict(start_cash=self.start_cash, margin_pct=self.margin_pct, buffer=self.buffer,
                    mult=self.mult, realized=self.realized, charges=self.charges,
                    blocked=self.blocked, lot_margin=self.lot_margin)

    @classmethod
    def from_dict(cls, d, margin_pct=None, buffer=None):
        """margin_pct / buffer from .env win, so a changed setting applies after a restart."""
        w = cls(d["start_cash"], margin_pct if margin_pct is not None else d.get("margin_pct", 0.10),
                buffer if buffer is not None else d.get("buffer", 0.25), d.get("mult", 1.0))
        w.realized, w.charges, w.blocked = d["realized"], d["charges"], d["blocked"]
        w.lot_margin = d.get("lot_margin", d.get("margin_per_lot", 0.0) if w.blocked else 0.0)
        return w


def trading_days_left(day: dt.date, expiry: dt.date, holidays=frozenset()) -> int:
    """Trading days strictly after `day` up to and including `expiry` (weekdays minus holidays)."""
    n, d = 0, day
    while d < expiry:
        d += dt.timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            n += 1
    return n


def pick_contract(day: dt.date, expiries: dict, roll_days: int, holidays=frozenset()) -> Optional[str]:
    """expiries: {contract_symbol: expiry_date}. Nearest contract with >= roll_days left."""
    for sym, exp in sorted(expiries.items(), key=lambda kv: kv[1]):
        if exp >= day and trading_days_left(day, exp, holidays) >= roll_days:
            return sym
    return None


def compute_levels(contract: str, bars: list[Bar]) -> Optional[Levels]:
    """bars: completed daily bars (oldest -> newest), today's bar NOT included."""
    if len(bars) < 4:
        return None
    b4, b2 = bars[-4:], bars[-2:]
    return Levels(contract, max(b.high for b in b4), min(b.low for b in b4),
                  max(b.high for b in b2), min(b.low for b in b2))


class Engine:
    def __init__(self, wallet: Wallet, lots: int = 2, roll_days: int = 10,
                 roll_time: dt.time = dt.time(21, 0), session_open: dt.time = dt.time(9, 0),
                 holidays=frozenset()):
        self.wallet = wallet
        self.lots = lots
        self.roll_days = roll_days
        self.roll_time = roll_time
        self.session_open = session_open
        self.holidays = holidays
        self.pos: Optional[Position] = None
        # per-day state
        self.day: Optional[dt.date] = None
        self.levels: dict[str, Levels] = {}
        self.active: Optional[str] = None
        self.day_open: dict[str, float] = {}
        self.first15: dict[str, list] = {}       # contract -> [high, low] of 09:00-09:15 ticks
        self.gap_entry = {1: None, -1: None}     # pending gap entries: side -> "wait" | price
        self.gap_stop = None                     # "wait" | None
        self.roll_pending = False
        self.entries_today = {1: 0, -1: 0}
        self.last_price: dict[str, float] = {}
        self.last_tick: str | None = None
        self._prev: float | None = None
        self.open_time: dt.time | None = None    # 09:00 normally, 17:00 on a 2nd-session-only day
        self.placed: set = set()                 # contracts whose 09:10 / 17:10 orders are placed

    # ------------------------------------------------------------------ morning
    def prepare_day(self, day: dt.date, expiries: dict, bars: dict) -> list[Event]:
        """bars: {contract: [Bar,...]} completed daily bars before `day`."""
        ev = []
        ts = dt.datetime.combine(day, dt.time(8, 55)).isoformat()
        self.day = day
        self.day_open, self.first15 = {}, {}
        self.gap_entry, self.gap_stop = {1: None, -1: None}, None
        self.entries_today = {1: 0, -1: 0}
        self.last_tick = None
        self.open_time, self.placed = None, set()
        self.active = pick_contract(day, expiries, self.roll_days, self.holidays)
        self.levels = {c: lv for c in bars if (lv := compute_levels(c, bars[c]))}
        if not self.active or self.active not in self.levels:
            ev.append(Event(ts, "INFO", note=f"No tradable contract / not enough history (active={self.active})"))
            return ev
        lv = self.levels[self.active]
        ev.append(Event(ts, "INFO", self.active,
                        note=f"Levels {self.active}: buy>{lv.buy_entry:.0f} sell<{lv.sell_entry:.0f} "
                             f"(4DHH {lv.hh4:.0f} 4DLL {lv.ll4:.0f} 2DHH {lv.hh2:.0f} 2DLL {lv.ll2:.0f})"))
        if self.pos:
            self._trail_stop(self.levels.get(self.pos.contract))
            self.roll_pending = self.pos.contract != self.active
            if self.roll_pending:
                ev.append(Event(ts, "INFO", self.pos.contract,
                                note=f"Rollover due today at {self.roll_time:%H:%M} -> {self.active}"))
        return ev

    def _stops(self, lv: Levels):
        p, s, base = self.pos.params, self.pos.side, self.pos.level_base
        if s == 1:
            sl1 = max(base * (1 - p["M"]), lv.ll2 * (1 - p["B"]))
            ref = lv.ll2 if p["sl2_days"] == 2 else lv.ll4
            sl2 = max(base if p["sl2_breakeven"] else base * (1 - p["M"]), ref * (1 - p["B"]))
        else:
            sl1 = min(base * (1 + p["M"]), lv.hh2 * (1 + p["B"]))
            ref = lv.hh2 if p["sl2_days"] == 2 else lv.hh4
            sl2 = min(base if p["sl2_breakeven"] else base * (1 + p["M"]), ref * (1 + p["B"]))
        return sl1, sl2

    def _trail_stop(self, lv: Optional[Levels]):
        if not lv or not self.pos:
            return
        sl1, sl2 = self._stops(lv)
        new = sl1 if self.pos.lots == 2 else sl2
        old = self.pos.sl
        if old is None:
            self.pos.sl = new
        else:  # only move in the trade's favour
            self.pos.sl = max(old, new) if self.pos.side == 1 else min(old, new)

    # ------------------------------------------------------------------ ticks
    # Session timing, from the course Excel ("Order Placing Time"):
    #   open O (09:00, or 17:00 when only the 2nd session trades):  target of a holding is live
    #   O + 10 min (09:10 / 17:10):  buy entry, sell entry and stop-loss of a holding are placed
    #   O + 15 min:  first 15-minute candle complete; gap orders are placed at O + 16 min
    # A level already crossed when its order would be placed is handled with the gap rule.
    PLACE_AFTER = dt.timedelta(minutes=10)
    GAP_CANDLE = dt.timedelta(minutes=15)
    GAP_ORDERS_AFTER = dt.timedelta(minutes=16)

    def on_tick(self, ts: dt.datetime, contract: str, price: float) -> list[Event]:
        ev: list[Event] = []
        if self.day is None or ts.date() != self.day or ts.time() < self.session_open:
            return ev
        self._prev = self.last_price.get(contract)     # price before this tick (None on the first)
        self.last_price[contract] = price
        self.last_tick = ts.isoformat(timespec="seconds")
        iso = self.last_tick

        # which session is trading today? (no morning prices before 17:00 = holiday morning)
        if self.open_time is None:
            self.open_time = dt.time(9, 0) if ts.time() < dt.time(17, 0) else dt.time(17, 0)
            ev.append(Event(iso, "INFO", contract, note=f"Session opened at {self.open_time:%H:%M}"))
        O = dt.datetime.combine(self.day, self.open_time)
        if ts < O:
            return ev
        placed_time = ts >= O + self.PLACE_AFTER
        gap_orders_time = ts >= O + self.GAP_ORDERS_AFTER

        # day open and the first 15-minute candle
        if contract not in self.day_open:
            self.day_open[contract] = price
            ev += self._on_open(iso, contract, price)
        if ts < O + self.GAP_CANDLE:
            h = self.first15.setdefault(contract, [price, price])
            h[0], h[1] = max(h[0], price), min(h[1], price)
        else:
            ev += self._resolve_gaps(iso, contract)

        # O + 10 min: orders get placed; a level already crossed by now follows the gap rule
        if placed_time and contract not in self.placed:
            self.placed.add(contract)
            ev += self._on_place(iso, contract, price)

        # rollover
        if self.pos and self.roll_pending and ts.time() >= self.roll_time and contract == self.active:
            old_px = self.last_price.get(self.pos.contract)
            if old_px:
                ev += self._rollover(iso, old_px, price)

        # manage the holding (target live from the open, stop-loss from O + 10 min)
        if self.pos and contract == self.pos.contract:
            ev += self._manage(iso, price, stop_live=placed_time)

        # entries: both sides are live; the opposite side can trigger while holding (flip)
        if contract == self.active and self.active in self.levels and placed_time:
            ev += self._try_entry(iso, price, gap_orders_time)
        return ev

    def _beyond(self, side, price, level):
        return price >= level if side == 1 else price <= level

    def _on_open(self, iso, contract, open_px):
        ev = []
        if contract == self.active and self.active in self.levels:
            lv = self.levels[contract]
            for side, lvl, word in ((1, lv.buy_entry, "up"), (-1, lv.sell_entry, "down")):
                if self.pos and self.pos.side == side:
                    continue
                if self._beyond(side, open_px, lvl) and open_px != lvl:
                    self.gap_entry[side] = "wait"
                    ev.append(Event(iso, "GAP", contract, side, price=open_px,
                                    note=f"Gap {word}: open {open_px:.0f} beyond {'buy' if side == 1 else 'sell'} "
                                         f"entry {lvl:.0f}, waiting for the 15-minute candle"))
        if self.pos and contract == self.pos.contract and self.pos.sl is not None:
            if self._beyond(-self.pos.side, open_px, self.pos.sl):
                self.gap_stop = "wait"
                ev.append(Event(iso, "GAP", contract, self.pos.side, price=open_px, trade=self.pos.opened,
                                note=f"Opened beyond stop-loss {self.pos.sl:.0f}: stop moves to the 15-minute candle"))
        return ev

    def _on_place(self, iso, contract, price):
        """Order placing time (09:10 / 17:10). Excel rule: a level the market already crossed since the
        open is treated like a gap (wait for the 15-minute candle)."""
        ev = []
        if contract == self.active and self.active in self.levels:
            lv = self.levels[contract]
            for side, lvl in ((1, lv.buy_entry), (-1, lv.sell_entry)):
                if self.gap_entry[side] is None and not (self.pos and self.pos.side == side) \
                        and self._beyond(side, price, lvl):
                    self.gap_entry[side] = "wait"
                    ev.append(Event(iso, "GAP", contract, side, price=price,
                                    note=f"Price {price:.0f} already beyond {'buy' if side == 1 else 'sell'} entry "
                                         f"{lvl:.0f} at order time: gap rule, waiting for the 15-minute candle"))
        p = self.pos
        if p and contract == p.contract and p.sl is not None and self.gap_stop is None \
                and self._beyond(-p.side, price, p.sl):
            self.gap_stop = "wait"
            ev.append(Event(iso, "GAP", contract, p.side, price=price, trade=p.opened,
                            note=f"Price {price:.0f} already beyond stop-loss {p.sl:.0f} at order time: "
                                 f"stop moves to the 15-minute candle"))
        return ev

    def _resolve_gaps(self, iso, contract):
        ev = []
        h = self.first15.get(contract)
        if not h:
            return ev
        hi, lo = h
        if contract == self.active:
            for side in (1, -1):
                if self.gap_entry[side] == "wait":
                    px = hi * (1 + GAP["B"]) if side == 1 else lo * (1 - GAP["B"])
                    self.gap_entry[side] = px
                    ev.append(Event(iso, "GAP", contract, side, price=_round(px),
                                    note=f"15-minute candle H {hi:.0f} L {lo:.0f}: gap "
                                         f"{'buy' if side == 1 else 'sell'} entry {px:.0f}"))
        if self.gap_stop == "wait" and self.pos and contract == self.pos.contract:
            self.pos.sl = lo * (1 - NORMAL["B"]) if self.pos.side == 1 else hi * (1 + NORMAL["B"])
            self.gap_stop = None
            ev.append(Event(iso, "GAP", contract, self.pos.side, price=_round(self.pos.sl), trade=self.pos.opened,
                            note=f"Stop-loss set from the 15-minute candle: {self.pos.sl:.0f}"))
        return ev

    def _fill(self, level, price, crossed_up: bool):
        """A resting trigger order fills at its level when price moves through it between ticks;
        it fills at the market price only if price was already beyond the level (a jump)."""
        prev = self._prev
        if prev is None:
            return price
        was_beyond = prev >= level if crossed_up else prev <= level
        return price if was_beyond else level

    def _manage(self, iso, price, stop_live):
        ev, p = [], self.pos
        if stop_live and p.sl is not None and self.gap_stop != "wait" and self._beyond(-p.side, price, p.sl):
            fill = self._fill(p.sl, price, crossed_up=(p.side == -1))
            ev.append(self._close(iso, fill, p.lots, "STOP", f"Stop-loss {p.sl:.0f} hit"))
            self.pos = None
            return ev
        if p.lots == 2 and self._beyond(p.side, price, p.target):
            fill = self._fill(p.target, price, crossed_up=(p.side == 1))
            ev.append(self._close(iso, fill, 1, "TARGET", f"Target {p.target:.0f} hit: 1 lot booked"))
            p.lots = 1
            lv = self.levels.get(p.contract)
            if lv:
                p.sl = self._stops(lv)[1]
                ev.append(Event(iso, "INFO", p.contract, p.side, 1, trade=p.opened,
                                note=f"Stop-loss 2 now {p.sl:.0f}"))
        return ev

    def _try_entry(self, iso, price, gap_orders_time):
        lv = self.levels[self.active]
        for side in (1, -1):
            if self.entries_today[side] or (self.pos and self.pos.side == side):
                continue
            g = self.gap_entry[side]
            if g == "wait" or (g is not None and not gap_orders_time):
                continue
            gap = g is not None
            trig = g if gap else (lv.buy_entry if side == 1 else lv.sell_entry)
            if self._beyond(side, price, trig):
                fill = self._fill(trig, price, crossed_up=(side == 1))
                ev = []
                if self.pos:   # opposite entry triggered while still holding: exit, then flip
                    ev.append(self._close(iso, fill, self.pos.lots, "STOP",
                                          f"Exit on opposite {'buy' if side == 1 else 'sell'} entry {trig:.0f}"))
                    self.pos = None
                return ev + self._open(iso, side, fill, lv, gap)
        return []

    def _open(self, iso, side, price, lv, gap):
        self.entries_today[side] += 1
        if not self.wallet.can_block(self.lots, price):
            return [Event(iso, "SKIP", self.active, side, self.lots, price,
                          note=f"Insufficient funds: need {self.wallet.margin_for(price, self.lots):.0f} "
                               f"margin at {price:.0f}, available {self.wallet.available:.0f}")]
        params = GAP if gap else NORMAL
        self.wallet.block(self.lots, price)
        chg = self.wallet.order_charges(price * self.wallet.mult * self.lots, is_sell=(side == -1))
        self.wallet.charges += chg
        self.pos = Position(side, self.active, price, price, self.lots, price * (1 + params["T"] * side),
                            None, params, iso, gap)
        self._trail_stop(lv)
        return [Event(iso, "ENTRY", self.active, side, self.lots, price, 0.0, chg, trade=iso,
                      note=f"{'BUY' if side==1 else 'SELL'} {self.lots} lots{' (gap rule)' if gap else ''} | "
                           f"target {self.pos.target:.0f} | SL {self.pos.sl:.0f}")]

    def _close(self, iso, price, lots, kind, note):
        p = self.pos
        pnl = (price - p.entry) * p.side * lots * self.wallet.mult
        chg = self.wallet.order_charges(price * self.wallet.mult * lots, is_sell=(p.side == 1))
        self.wallet.realized += pnl
        self.wallet.charges += chg
        self.wallet.release(lots)
        p.realized += pnl
        return Event(iso, kind, p.contract, p.side, lots, price, round(pnl, 2), round(chg, 2), note, p.opened,
                     round(p.realized, 2))

    def _rollover(self, iso, old_px, new_px):
        p = self.pos
        ev = [self._close(iso, old_px, p.lots, "ROLL_OUT", f"Rollover: exit {p.contract}")]
        self.wallet.block(p.lots, new_px)       # fresh margin at the new contract's price
        chg = self.wallet.order_charges(new_px * self.wallet.mult * p.lots, is_sell=(p.side == -1))
        self.wallet.charges += chg
        # same target / stop numbers as the old contract (course rule)
        p.contract, p.entry = self.active, new_px
        self.roll_pending = False
        ev.append(Event(iso, "ROLL_IN", p.contract, p.side, p.lots, new_px, 0.0, round(chg, 2),
                        f"Rollover: re-enter {p.contract}, target {p.target:.0f} SL {p.sl:.0f} unchanged", p.opened))
        return ev

    # ------------------------------------------------------------------ status
    def unrealized(self) -> float:
        if not self.pos:
            return 0.0
        px = self.last_price.get(self.pos.contract)
        return 0.0 if px is None else (px - self.pos.entry) * self.pos.side * self.pos.lots * self.wallet.mult

    def snapshot(self) -> dict:
        lv = self.levels.get(self.active) if self.active else None
        return dict(
            day=self.day.isoformat() if self.day else None,
            active=self.active,
            levels=None if not lv else dict(contract=lv.contract, hh4=lv.hh4, ll4=lv.ll4, hh2=lv.hh2, ll2=lv.ll2,
                                            buy_entry=_round(lv.buy_entry), sell_entry=_round(lv.sell_entry),
                                            buy_target=_round(lv.buy_entry * (1 + NORMAL["T"])),
                                            sell_target=_round(lv.sell_entry * (1 - NORMAL["T"]))),
            gap_entry={str(k): v for k, v in self.gap_entry.items()},
            gap_stop=self.gap_stop,
            roll_pending=self.roll_pending,
            session=self.open_time.strftime("%H:%M") if self.open_time else None,
            orders_placed=self.active in self.placed if self.active else False,
            position=None if not self.pos else {**asdict(self.pos),
                                                "target": _round(self.pos.target), "sl": _round(self.pos.sl)},
            last_price=self.last_price,
            unrealized=round(self.unrealized(), 2),
            wallet={**self.wallet.to_dict(), "available": round(self.wallet.available, 2),
                    "equity": round(self.wallet.start_cash + self.wallet.realized - self.wallet.charges
                                    + self.unrealized(), 2)},
        )

    # persistence of the open position across restarts
    def save_state(self) -> dict:
        return dict(pos=asdict(self.pos) if self.pos else None, wallet=self.wallet.to_dict(),
                    day=self.day.isoformat() if self.day else None, entries_today=self.entries_today,
                    roll_pending=self.roll_pending,
                    intraday=dict(day=self.day.isoformat() if self.day else None, last_tick=self.last_tick,
                                  day_open=self.day_open, first15=self.first15,
                                  gap_entry={str(k): v for k, v in self.gap_entry.items()},
                                  gap_stop=self.gap_stop,
                                  entries_today={str(k): v for k, v in self.entries_today.items()},
                                  last_price=self.last_price,
                                  open_time=self.open_time.isoformat() if self.open_time else None,
                                  placed=sorted(self.placed)))

    def restore_intraday(self, d: dict | None) -> str | None:
        """After prepare_day(): if the saved state is from today, put back today's open, 09:15 candle,
        gap flags and entry counts. Returns the last processed tick time (replay resumes after it)."""
        if not d or not self.day or d.get("day") != self.day.isoformat():
            return None
        self.day_open = d.get("day_open", {})
        self.first15 = d.get("first15", {})
        self.gap_entry = {int(k): v for k, v in d.get("gap_entry", {}).items()} or {1: None, -1: None}
        self.gap_stop = d.get("gap_stop")
        self.entries_today = {int(k): v for k, v in d.get("entries_today", {}).items()} or {1: 0, -1: 0}
        self.last_price.update(d.get("last_price", {}))
        self.last_tick = d.get("last_tick")
        self.open_time = dt.time.fromisoformat(d["open_time"]) if d.get("open_time") else None
        self.placed = set(d.get("placed", []))
        return self.last_tick

    def load_state(self, d: dict):
        if not d:
            return
        self.wallet = Wallet.from_dict(d["wallet"], self.wallet.margin_pct, self.wallet.buffer)
        self.pos = Position(**d["pos"]) if d.get("pos") else None
        self.roll_pending = d.get("roll_pending", False)
