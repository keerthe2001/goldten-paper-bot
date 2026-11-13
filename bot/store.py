"""SQLite storage shared by the bot (writer) and the API (reader)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, kind TEXT, contract TEXT, side INTEGER, lots INTEGER,
    price REAL, pnl REAL, charges REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY, equity REAL, realized REAL, unrealized REAL, charges REAL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT, updated REAL
);
"""


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = str(path)
        if readonly:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")   # lets the API read while the bot writes
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row

    # ---- writes (bot) ----
    def add_event(self, e) -> int:
        cur = self.db.execute(
            "INSERT INTO events (ts, kind, contract, side, lots, price, pnl, charges, note) VALUES (?,?,?,?,?,?,?,?,?)",
            (e.ts, e.kind, e.contract, e.side, e.lots, e.price, e.pnl, e.charges, e.note))
        self.db.commit()
        return cur.lastrowid

    def set(self, key: str, value):
        self.db.execute("INSERT INTO kv (key, value, updated) VALUES (?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                        (key, json.dumps(value, default=str), time.time()))
        self.db.commit()

    def add_equity(self, ts, equity, realized, unrealized, charges):
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?)",
                        (ts, equity, realized, unrealized, charges))
        self.db.commit()

    # ---- reads (API and bot restart) ----
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value, updated FROM kv WHERE key=?", (key,)).fetchone()
        return default if not row else json.loads(row["value"])

    def get_with_time(self, key: str):
        row = self.db.execute("SELECT value, updated FROM kv WHERE key=?", (key,)).fetchone()
        return (None, None) if not row else (json.loads(row["value"]), row["updated"])

    def events(self, limit=200, kinds=None):
        q, args = "SELECT * FROM events", []
        if kinds:
            q += f" WHERE kind IN ({','.join('?' * len(kinds))})"
            args = list(kinds)
        q += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(q, args + [limit])]

    def equity_curve(self, limit=2000):
        rows = self.db.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]
