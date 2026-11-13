"""Telegram alerts and Google Sheets rows. Both are optional: if their settings
are missing, they quietly do nothing, so the bot still runs."""
from __future__ import annotations

import logging
import threading
import queue

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
    """Appends one row per event to a worksheet (created if missing).
    Runs in a background thread so a slow Google API never blocks price handling."""

    HEADER = ["Time", "Event", "Contract", "Side", "Lots", "Price", "P/L", "Charges", "Note",
              "Wallet available", "Equity"]

    def __init__(self, creds_file: str | None, sheet_id: str | None, tab: str = "Paper Trades"):
        self.ok = bool(creds_file and sheet_id)
        self.q: queue.Queue = queue.Queue()
        if not self.ok:
            return
        try:
            import gspread
            gc = gspread.service_account(filename=creds_file)
            sh = gc.open_by_key(sheet_id)
            try:
                self.ws = sh.worksheet(tab)
            except gspread.WorksheetNotFound:
                self.ws = sh.add_worksheet(title=tab, rows=2000, cols=len(self.HEADER))
                self.ws.append_row(self.HEADER)
        except Exception as exc:
            log.error("Google Sheets disabled: %s", exc)
            self.ok = False
            return
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, e, wallet_available: float, equity: float):
        if self.ok:
            self.q.put([e.ts, e.kind, e.contract, SIDE.get(e.side, ""), e.lots, e.price, e.pnl,
                        e.charges, e.note, round(wallet_available, 2), round(equity, 2)])

    def _worker(self):
        while True:
            row = self.q.get()
            for attempt in range(3):
                try:
                    self.ws.append_row(row, value_input_option="USER_ENTERED")
                    break
                except Exception as exc:
                    log.warning("Sheets append failed (try %d): %s", attempt + 1, exc)
                    threading.Event().wait(5 * (attempt + 1))
