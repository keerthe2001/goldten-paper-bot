"""Telegram alerts and the Google Sheet trade log. Both are optional: if their settings are
missing they quietly do nothing, so the bot still runs."""
from __future__ import annotations

import logging
import queue
import threading

import requests

log = logging.getLogger("notify")

SIDE = {1: "BUY", -1: "SELL", 0: ""}


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None):
        self.ok = bool(token and chat_id)
        self.url = f"https://api.telegram.org/bot{token}/sendMessage" if self.ok else None
        self.chat_id = chat_id

    def send(self, text: str):
        if not self.ok:
            return
        try:
            requests.post(self.url, json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except Exception as exc:  # never let an alert failure stop the bot
            log.warning("Telegram send failed: %s", exc)


class Sheets:
    """Fills the course's "Gold Ten Fut" tracking tab: one row per trade, in the first empty row.
    Only the input columns are written (the ones you would type by hand); the tab's own formulas
    (MSL, new stop-loss, trigger / price SL 1 & 2) keep calculating. Runs in a background thread
    so a slow Google API never blocks price handling."""

    # input columns of the Gold Ten Fut tab
    COL = {"DATE": "A", "TRADE": "B", "ENTRY": "C", "TARGET": "D", "INITIAL SL": "E",
           "2D VALUE": "H", "4D VALUE": "K", "EXIT 1": "Q", "EXIT 2": "R", "TOTAL P/L": "S", "ROLLOVER": "T"}
    FIRST_ROW = 3     # rows 1-2 are headers

    def __init__(self, creds_file: str | None, sheet_id: str | None, tab: str = "Gold Ten Fut",
                 rows: dict | None = None):
        self.ok = bool(creds_file and sheet_id)
        self.q: queue.Queue = queue.Queue()
        self.rows: dict[str, int] = dict(rows or {})     # trade id -> sheet row (saved by the bot)
        if not self.ok:
            return
        try:
            import gspread
            self.ws = gspread.service_account(filename=creds_file).open_by_key(sheet_id).worksheet(tab)
        except Exception as exc:
            log.error("Google Sheets disabled (need a tab named %r): %s", tab, exc)
            self.ok = False
            return
        threading.Thread(target=self._worker, daemon=True, name="sheets").start()

    @staticmethod
    def _levels(side, lv):
        lv = lv or {}
        return (lv.get("ll2"), lv.get("ll4")) if side == 1 else (lv.get("hh2"), lv.get("hh4"))

    # ---- called by the bot ----
    def trade_event(self, e, pos: dict | None, levels: dict | None):
        """e: strategy Event; pos: the open position after the event (or None); levels: today's levels."""
        if not self.ok or not e.trade:
            return
        if e.kind == "ENTRY":
            v2, v4 = self._levels(e.side, levels)
            self.q.put(("new", e.trade, {
                "DATE": e.ts[:10], "TRADE": SIDE[e.side], "ENTRY": round(e.price),
                "TARGET": round((pos or {}).get("target") or 0), "INITIAL SL": round((pos or {}).get("sl") or 0),
                "2D VALUE": v2, "4D VALUE": v4}))
            return
        upd = {}
        if e.kind == "TARGET":
            upd = {"EXIT 1": e.pnl, "TOTAL P/L": e.total}
        elif e.kind == "STOP":
            upd = {"EXIT 1": round(e.pnl / 2, 2), "EXIT 2": round(e.pnl / 2, 2)} if e.lots == 2 else {"EXIT 2": e.pnl}
            upd["TOTAL P/L"] = e.total
        elif e.kind == "ROLL_OUT":
            upd = {"TOTAL P/L": e.total}
        elif e.kind == "ROLL_IN":
            upd = {"ROLLOVER": f"{e.ts[:10]} {e.contract} @ {e.price:.0f}"}
        if upd:
            self.q.put(("update", e.trade, upd))

    def update_levels(self, trade: str, side: int, levels: dict | None):
        """Each morning: today's 2D / 4D levels into H and K, so the tab's new-stop-loss formulas trail."""
        if self.ok and trade:
            v2, v4 = self._levels(side, levels)
            self.q.put(("update", trade, {"2D VALUE": v2, "4D VALUE": v4}))

    # ---- background worker ----
    def _next_empty_row(self):
        col_b = self.ws.col_values(2)              # TRADE column
        r = self.FIRST_ROW
        while r <= len(col_b) and str(col_b[r - 1]).strip():
            r += 1
        return r

    def _write(self, row, data):
        self.ws.batch_update([{"range": f"{self.COL[k]}{row}", "values": [[v]]}
                              for k, v in data.items() if v is not None], value_input_option="USER_ENTERED")

    def _worker(self):
        while True:
            op, trade, data = self.q.get()
            for attempt in range(3):
                try:
                    if op == "new":
                        if trade not in self.rows:
                            self.rows[trade] = self._next_empty_row()
                        self._write(self.rows[trade], data)
                    else:
                        row = self.rows.get(trade)
                        if not row:
                            log.warning("Sheets: no row recorded for trade %s", trade)
                            break
                        self._write(row, data)
                    break
                except Exception as exc:
                    log.warning("Sheets %s failed (try %d): %s", op, attempt + 1, exc)
                    threading.Event().wait(5 * (attempt + 1))
