"""Read-only API for the dashboard.

It never trades and never writes: it only reads the SQLite file and the log file
that the bot writes. If this crashes, the bot keeps running.

Run:  uvicorn api.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse

from bot.store import Store

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DB = ROOT / os.getenv("DB_PATH", "data/goldten.db")
LOG = ROOT / os.getenv("LOG_DIR", "logs") / "bot.log"
STALE_AFTER = 20  # seconds without a heartbeat -> bot shown as stopped

app = FastAPI(title="Gold Ten Paper Bot API")


def store() -> Store | None:
    try:
        return Store(DB, readonly=True)
    except sqlite3.OperationalError:
        return None


@app.get("/api/status")
def status():
    s = store()
    if not s:
        return {"alive": False, "reason": "No database yet: the bot has not started."}
    beat, updated = s.get_with_time("heartbeat")
    age = None if updated is None else round(time.time() - updated, 1)
    return {
        "alive": age is not None and age < STALE_AFTER,
        "heartbeat_age_s": age,
        "heartbeat": beat,
        "snapshot": s.get("snapshot"),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/config")
def config():
    """What the dashboard needs to embed the tracking sheet (behind the Nginx login)."""
    sid = os.getenv("GOOGLE_SHEET_ID") or ""
    gid = os.getenv("GOOGLE_SHEET_GID") or ""
    return {"sheet_url": f"https://docs.google.com/spreadsheets/d/{sid}/edit?rm=minimal" + (f"#gid={gid}" if gid else "")
            if sid else None}


@app.get("/api/events")
def events(limit: int = Query(200, le=2000), trades_only: bool = False):
    s = store()
    if not s:
        return []
    kinds = ["ENTRY", "TARGET", "STOP", "ROLL_OUT", "ROLL_IN", "SKIP"] if trades_only else None
    return s.events(limit, kinds)


@app.get("/api/equity")
def equity():
    s = store()
    return s.equity_curve() if s else []


@app.get("/api/logs")
def logs(lines: int = Query(200, le=2000)):
    if not LOG.exists():
        return {"lines": []}
    with LOG.open(encoding="utf-8", errors="replace") as f:
        return {"lines": list(deque(f, maxlen=lines))}


@app.get("/api/logs/stream")
async def logs_stream():
    """Server-Sent Events: pushes each new log line to the browser as it is written."""
    async def gen():
        while not LOG.exists():
            yield ": waiting for log\n\n"
            await asyncio.sleep(2)
        with LOG.open(encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)                      # start at the end, like `tail -f`
            idle = 0
            while True:
                line = f.readline()
                if line:
                    idle = 0
                    yield f"data: {line.rstrip()}\n\n"
                else:
                    idle += 1
                    if idle % 30 == 0:
                        yield ": keep-alive\n\n"   # stops proxies closing an idle connection
                    if LOG.stat().st_size < f.tell():  # log rotated -> reopen
                        f.close()
                        f = LOG.open(encoding="utf-8", errors="replace")
                    await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Local development convenience: serve the dashboard too (in production Nginx does this).
WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/{name}")
def static(name: str):
    path = (WEB / name).resolve()
    if path.parent == WEB.resolve() and path.is_file():
        return FileResponse(path)
    return FileResponse(WEB / "index.html")
