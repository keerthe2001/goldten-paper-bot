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
    """One row per trade, laid out like the course's Futures Tracking sheet. The row is added on
    entry and filled in as the trade progresses (stop-loss trail, exit 1, exit 2, rollover, total).
    Runs in a background thread so a slow Google API never blocks price handling."""

    HEADER = ["DATE", "TRADE", "CONTRACT", "ENTRY", "TARGET", "INITIAL SL", "MSL",
              "2DHH/2DLL", "4DHH/4DLL", "CURRENT SL", "EXIT 1", "EXIT 2", "TOTAL P/L",
              "ROLLOVER", "STATUS", "NOTES", "TRADE ID"]
    COL = {name: i + 1 for i, name in enumerate(HEADER)}

    def __init__(self, creds_file: str | None, sheet_id: str | None, tab: str = "Gold Ten Trades"):
        self.ok = bool(creds_file and sheet_id)
        self.q: queue.Queue = queue.Queue()
        self.rows: dict[str, int] = {}
        if not self.ok:
            return
        try:
            import gspread
            sh = gspread.service_account(filename=creds_file).open_by_key(sheet_id)
            try:
                self.ws = sh.worksheet(tab)
            except gspread.WorksheetNotFound:
                self.ws = sh.add_worksheet(title=tab, rows=1000, cols=len(self.HEADER))
                self.ws.append_row(self.HEADER)
        except Exception as exc:
            log.error("Google Sheets disabled: %s", exc)
            self.ok = False
            return
        threading.Thread(target=self._worker, daemon=True, name="sheets").start()

    # ---- called by the bot ----
    def trade_event(self, e, pos: dict | None, levels: dict | None):
        """e: strategy Event; pos: the open position after the event (or None); levels: today's levels."""
        if not self.ok or not e.trade:
            return
        upd = {}
        if e.kind == "ENTRY":
            ref2, ref4 = ("2DLL", "4DLL") if e.side == 1 else ("2DHH", "4DHH")
            lv = levels or {}
            msl = e.price * (1 - 0.015 * e.side) if not (pos or {}).get("gap_trade") else e.price * (1 - 0.0125 * e.side)
            row = [e.ts[:10], SIDE[e.side], e.contract, round(e.price, 2), (pos or {}).get("target"),
                   (pos or {}).get("sl"), round(msl, 2),
                   f"{ref2} {lv.get('ll2' if e.side == 1 else 'hh2', '')}",
                   f"{ref4} {lv.get('ll4' if e.side == 1 else 'hh4', '')}",
                   (pos or {}).get("sl"), "", "", 0, "", "OPEN", e.note, e.trade]
            self.q.put(("append", e.trade, row))
            return
        if e.kind == "TARGET":
            upd = {"EXIT 1": e.pnl, "TOTAL P/L": e.total, "STATUS": "TARGET HIT, 1 LOT OPEN"}
        elif e.kind == "STOP":
            if e.lots == 2:
                upd = {"EXIT 1": round(e.pnl / 2, 2), "EXIT 2": round(e.pnl / 2, 2)}
            else:
                upd = {"EXIT 2": e.pnl}
            upd.update({"TOTAL P/L": e.total, "STATUS": "CLOSED", "NOTES": e.note})
        elif e.kind == "ROLL_OUT":
            upd = {"TOTAL P/L": e.total}
        elif e.kind == "ROLL_IN":
            upd = {"CONTRACT": e.contract, "ROLLOVER": f"{e.ts[:10]} -> {e.contract} @ {e.price:.0f}"}
        if pos and pos.get("opened") == e.trade:
            upd.setdefault("CURRENT SL", pos.get("sl"))
        if upd:
            self.q.put(("update", e.trade, upd))

    def update_sl(self, trade: str, sl: float):
        """Daily stop-loss trail (the 'NEW STOPLOSS' columns of the tracking sheet)."""
        if self.ok and trade:
            self.q.put(("update", trade, {"CURRENT SL": sl}))

    # ---- background worker ----
    def _row(self, trade):
        if trade not in self.rows:
            cell = self.ws.find(trade, in_column=self.COL["TRADE ID"])
            if cell:
                self.rows[trade] = cell.row
        return self.rows.get(trade)

    def _worker(self):
        import gspread.utils as gu
        while True:
            op, trade, data = self.q.get()
            for attempt in range(3):
                try:
                    if op == "append":
                        res = self.ws.append_row(data, value_input_option="USER_ENTERED")
                        rng = res.get("updates", {}).get("updatedRange", "")
                        if "!" in rng:
                            self.rows[trade] = gu.a1_to_rowcol(rng.split("!")[1].split(":")[0])[0]
                    else:
                        row = self._row(trade)
                        if not row:
                            log.warning("Sheets: trade %s not found", trade)
                            break
                        self.ws.batch_update([{"range": gu.rowcol_to_a1(row, self.COL[k]), "values": [[v]]}
                                              for k, v in data.items()], value_input_option="USER_ENTERED")
                    break
                except Exception as exc:
                    log.warning("Sheets %s failed (try %d): %s", op, attempt + 1, exc)
                    threading.Event().wait(5 * (attempt + 1))
