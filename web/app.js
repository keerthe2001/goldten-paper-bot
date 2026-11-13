// Dashboard logic: ask the API for JSON every few seconds and draw it.
const $ = (id) => document.getElementById(id);
const inr = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
  : (v < 0 ? "−₹" : "₹") + Math.abs(Number(v)).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const px = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const signed = (el, v) => { el.textContent = inr(v); el.className = "num " + (v > 0 ? "up" : v < 0 ? "down" : ""); };
const SIDE = { "1": "BUY", "-1": "SELL", "0": "" };

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

async function refreshStatus() {
  try {
    const s = await getJSON("api/status");
    const pill = $("alive"), hb = s.heartbeat || {};
    if (!s.alive) { pill.textContent = "Bot stopped"; pill.className = "pill bad"; }
    else if (hb.session === "open") { pill.textContent = "Monitoring"; pill.className = "pill ok"; }
    else { pill.textContent = hb.session === "holiday" ? "Market holiday" : "Market closed"; pill.className = "pill idle"; }
    $("beat").textContent = s.heartbeat_age_s == null ? (s.reason || "") :
      `heartbeat ${s.heartbeat_age_s}s ago · ${hb.mode || ""}`;

    const snap = s.snapshot || {};
    const lv = snap.levels;
    $("contract").textContent = "MCX GOLDTEN · " + (snap.active || "—");
    const last = snap.last_price || {};
    $("ltp").textContent = px(last[snap.active]);
    $("feed").textContent = hb.market_time ? `${hb.feed} feed · market time ${hb.market_time.replace("T", " ")}` : "—";

    if (lv) {
      $("buy").textContent = px(lv.buy_entry); $("buyT").textContent = px(lv.buy_target);
      $("sell").textContent = px(lv.sell_entry); $("sellT").textContent = px(lv.sell_target);
      $("d4").textContent = `${px(lv.hh4)} / ${px(lv.ll4)}`; $("d2").textContent = `${px(lv.hh2)} / ${px(lv.ll2)}`;
    }
    const flags = [];
    for (const [side, v] of Object.entries(snap.gap_entry || {})) {
      if (v === "wait") flags.push(`Gap ${side === "1" ? "up" : "down"}: waiting for 09:15 candle`);
      else if (v) flags.push(`Gap ${side === "1" ? "buy" : "sell"} entry ${px(v)}`);
    }
    if (snap.gap_stop === "wait") flags.push("Opened beyond stop: waiting for 09:15 candle");
    if (snap.roll_pending) flags.push("Rollover due today");
    $("flags").replaceChildren(...flags.map((t) => Object.assign(document.createElement("span"), { className: "flag", textContent: t })));

    const p = snap.position;
    if (p) {
      $("pos").className = "";
      $("pos").innerHTML = `<div class="posline"><span class="side ${p.side === 1 ? "up" : "down"}"></span><span class="num" id="pu"></span></div>
        <dl class="grid2"><div><dt>Entry</dt><dd class="num" id="pe"></dd></div><div><dt>Target</dt><dd class="num" id="pt"></dd></div>
        <div><dt>Stop-loss</dt><dd class="num" id="ps"></dd></div><div><dt>Contract</dt><dd class="num small" id="pc"></dd></div></dl>`;
      $("pos").querySelector(".side").textContent = `${SIDE[p.side]} ${p.lots} lot${p.lots > 1 ? "s" : ""}${p.gap_trade ? " · gap rule" : ""}`;
      signed($("pu"), snap.unrealized);
      $("pe").textContent = px(p.entry); $("pt").textContent = p.lots === 2 ? px(p.target) : "Booked";
      $("ps").textContent = px(p.sl); $("pc").textContent = p.contract;
    } else {
      $("pos").className = "empty"; $("pos").textContent = "Flat: waiting for a breakout.";
    }

    const w = snap.wallet || {};
    $("equity").textContent = inr(w.equity); $("avail").textContent = inr(w.available);
    $("blocked").textContent = inr(w.blocked); signed($("realized"), w.realized);
    signed($("unreal"), snap.unrealized); $("charges").textContent = inr(w.charges, 2);
  } catch (e) {
    $("alive").textContent = "Dashboard offline"; $("alive").className = "pill bad";
    $("beat").textContent = "Cannot reach the API";
  }
}

async function refreshTrades() {
  try {
    const rows = await getJSON("api/events?trades_only=true&limit=100");
    const tb = $("trades");
    if (!rows.length) return;
    tb.replaceChildren(...rows.map((r) => {
      const tr = document.createElement("tr");
      const cells = [r.ts.replace("T", " ").slice(5, 16), r.kind, SIDE[r.side], r.lots, px(r.price),
        r.pnl ? inr(r.pnl) : "", r.note];
      cells.forEach((c, i) => {
        const td = document.createElement("td");
        td.textContent = c ?? "";
        if (i === 4 || i === 5) td.className = "r num" + (i === 5 && r.pnl ? (r.pnl > 0 ? " up" : " down") : "");
        if (i === 6) td.className = "note";
        tr.appendChild(td);
      });
      return tr;
    }));
  } catch (e) { /* status card already shows the problem */ }
}

async function refreshCurve() {
  try {
    const pts = await getJSON("api/equity");
    const c = $("curve"), ctx = c.getContext("2d");
    const dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = 90;
    c.width = W * dpr; c.height = H * dpr; ctx.scale(dpr, dpr); ctx.clearRect(0, 0, W, H);
    if (pts.length < 2) return;
    const ys = pts.map((p) => p.equity), lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const css = getComputedStyle(document.documentElement);
    ctx.strokeStyle = css.getPropertyValue("--gold"); ctx.lineWidth = 1.6; ctx.beginPath();
    pts.forEach((p, i) => {
      const x = (i / (pts.length - 1)) * (W - 4) + 2, y = H - 6 - ((p.equity - lo) / span) * (H - 12);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  } catch (e) { /* ignore */ }
}

function startLogs() {
  const box = $("log");
  const add = (line) => {
    box.textContent += line + "\n";
    if (box.textContent.length > 60000) box.textContent = box.textContent.slice(-40000);
    if ($("follow").checked) box.scrollTop = box.scrollHeight;
  };
  getJSON("api/logs?lines=150").then((d) => d.lines.forEach((l) => add(l.trimEnd()))).catch(() => {});
  const es = new EventSource("api/logs/stream");
  es.onmessage = (ev) => add(ev.data);
  es.onerror = () => { /* the browser reconnects automatically */ };
}

refreshStatus(); refreshTrades(); refreshCurve(); startLogs();
setInterval(refreshStatus, 3000);
setInterval(refreshTrades, 10000);
setInterval(refreshCurve, 60000);
