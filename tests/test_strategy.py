"""Rule-by-rule tests for the strategy engine.  Run:  python -m pytest -q"""
import datetime as dt

import pytest

from bot.strategy import Bar, Engine, Wallet, compute_levels, pick_contract, trading_days_left

DAY = dt.date(2026, 11, 13)          # a Friday
NOV, DEC = "GOLDTEN26NOVFUT", "GOLDTEN26DECFUT"
EXP = {NOV: dt.date(2026, 11, 30), DEC: dt.date(2026, 12, 31)}


def bars(highs_lows, start=dt.date(2026, 11, 9)):
    out, d = [], start
    for h, l in highs_lows:
        out.append(Bar(d, (h + l) / 2, h, l, (h + l) / 2))
        d += dt.timedelta(days=1)
    return out


# 4 days: 4DHH = 130000, 4DLL = 126000, 2DHH = 129000, 2DLL = 127000
HIST = bars([(130000, 126000), (129500, 126500), (129000, 127000), (128800, 127200)])


def engine(cash=60000):
    return Engine(Wallet(cash, margin_pct=0.10, buffer=0.25), lots=2, roll_days=10)


def t(hh, mm, ss=0, day=DAY):
    return dt.datetime.combine(day, dt.time(hh, mm, ss))


def ticks(e, contract, seq):
    out = []
    for (hh, mm), price in seq:
        out += e.on_tick(t(hh, mm), contract, price)
    return out


def kinds(events):
    return [x.kind for x in events if x.kind not in ("INFO",)]


def test_levels_and_contract_choice():
    lv = compute_levels(NOV, HIST)
    assert (lv.hh4, lv.ll4, lv.hh2, lv.ll2) == (130000, 126000, 129000, 127000)
    assert lv.buy_entry == pytest.approx(130156)          # 130000 * 1.0012
    assert lv.sell_entry == pytest.approx(125848.8)       # 126000 * 0.9988
    assert trading_days_left(DAY, EXP[NOV]) == 11
    assert pick_contract(DAY, EXP, 10) == NOV
    assert pick_contract(dt.date(2026, 11, 17), EXP, 10) == DEC   # only 9 days left -> roll


def test_long_entry_target_then_stop2():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])       # crosses 130156
    assert kinds(ev) == ["ENTRY"] and e.pos.side == 1 and e.pos.lots == 2
    E = 130000 * 1.0012                                           # fills at the trigger, not the tick
    assert e.pos.entry == pytest.approx(E)
    assert e.pos.target == pytest.approx(E * 1.015)
    assert e.pos.sl == pytest.approx(max(E * 0.985, 127000 * 0.9988))   # SL1
    assert e.wallet.blocked == pytest.approx(2 * E * 0.10 * 1.25)      # 10% margin + 25% buffer
    ev = ticks(e, NOV, [((12, 0), 132200)])                          # target 132153
    assert kinds(ev) == ["TARGET"] and e.pos.lots == 1
    assert e.pos.sl == pytest.approx(max(E * 0.985, 126000 * 0.9988))   # SL2 uses 4DLL
    ev = ticks(e, NOV, [((15, 0), 128200)])
    assert kinds(ev) == ["STOP"] and e.pos is None
    assert e.wallet.blocked == 0
    sl2 = max(E * 0.985, 126000 * 0.9988)
    assert e.wallet.realized == pytest.approx((E * 1.015 - E) + (sl2 - E))   # target and stop at their levels


def test_short_entry_stop_both_lots():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 127000), ((9, 10), 127000), ((11, 0), 125800), ((13, 0), 127800)])
    assert kinds(ev) == ["ENTRY", "STOP"]
    E = 126000 * 0.9988                                   # sell trigger 125,848.8
    SL = min(E * 1.015, 129000 * 1.0012)                  # SL1 = 127,736.5
    stop = [x for x in ev if x.kind == "STOP"][0]
    assert stop.price == pytest.approx(SL)                # filled at the stop level, not the 127,800 tick
    assert stop.lots == 2 and stop.pnl == pytest.approx((E - SL) * 2, abs=0.01)


def test_gap_up_waits_for_first_15_minutes():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 130500), ((9, 5), 130900), ((9, 10), 130300)])
    assert kinds(ev) == ["GAP"] and e.pos is None                  # no entry at the open
    ev = ticks(e, NOV, [((9, 16), 130700)])                         # 09:15 high 130900 -> entry 131063.6
    assert kinds(ev) == ["GAP"] and e.pos is None
    assert e.gap_entry[1] == pytest.approx(130900 * 1.00125)
    ev = ticks(e, NOV, [((9, 40), 131100)])
    assert kinds(ev) == ["ENTRY"] and e.pos.gap_trade
    G = 130900 * 1.00125
    assert e.pos.entry == pytest.approx(G)                         # fills at the gap trigger
    assert e.pos.target == pytest.approx(G * 1.0125)               # gap rule: 1.25% target


def test_gap_beyond_stop_uses_0915_candle():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])            # long, SL1 ~128247
    nxt = dt.date(2026, 11, 16)
    e.prepare_day(nxt, EXP, {NOV: HIST})
    ev = [*e.on_tick(t(9, 0, day=nxt), NOV, 127800)]               # opens below the stop
    assert kinds(ev) == ["GAP"] and e.pos is not None              # not stopped at the open
    e.on_tick(t(9, 10, day=nxt), NOV, 127600)
    ev = e.on_tick(t(9, 16, day=nxt), NOV, 127900)                  # stop -> 09:15 low 127600 * 0.9988
    assert e.pos.sl == pytest.approx(127600 * 0.9988)
    ev = e.on_tick(t(10, 0, day=nxt), NOV, 127400)
    assert kinds(ev) == ["STOP"]


def test_rollover_keeps_same_target_and_stop_numbers():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])
    target, sl = e.pos.target, e.pos.sl
    roll_day = dt.date(2026, 11, 17)                               # NOV now has < 10 days left
    dec_hist = bars([(h * 1.006, l * 1.006) for h, l in [(130000, 126000), (129500, 126500),
                                                          (129000, 127000), (128800, 127200)]])
    e.prepare_day(roll_day, EXP, {NOV: HIST, DEC: dec_hist})
    assert e.roll_pending and e.active == DEC
    e.on_tick(t(20, 59, day=roll_day), NOV, 130500)
    ev = e.on_tick(t(21, 0, day=roll_day), DEC, 131300)
    assert kinds(ev) == ["ROLL_OUT", "ROLL_IN"]
    assert e.pos.contract == DEC and e.pos.entry == 131300
    assert e.pos.target == target and e.pos.sl == sl               # course rule: same numbers


def test_insufficient_funds_skips_entry():
    e = engine(cash=30000)                                          # 2 lots need ~32,550
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])
    assert kinds(ev) == ["SKIP"] and e.pos is None


def test_stop_only_moves_in_favour():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])
    sl = e.pos.sl
    lower = bars([(129000, 120000)] * 4)                            # 2DLL collapses
    e.prepare_day(dt.date(2026, 11, 16), EXP, {NOV: lower})
    assert e.pos.sl == sl                                            # never loosened


def test_margin_is_dynamic_and_follows_price():
    w = Wallet(60000, margin_pct=0.10, buffer=0.25)
    assert w.margin_for(130000, 2) == pytest.approx(32500)
    assert w.margin_for(150000, 2) == pytest.approx(37500)      # gold up -> more margin
    w.block(2, 130000); w.release(1)
    assert w.blocked == pytest.approx(16250)                    # releases what was blocked per lot
    w.release(1)
    assert w.blocked == 0


def test_todays_case_gap_down_late_start_no_trade_above_0915_low():
    """20 Nov 2026: sell level 128,845, open 127,800 (gap down), 09:15 low 122,738.
    Excel rule: sell only below 122,738 x 0.99875 = 122,585. At 124,402 there must be NO trade."""
    hist = bars([(138181, 131000), (137000, 130500), (135269, 129000), (134000, 129500)])
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: hist})
    ev = ticks(e, NOV, [((9, 0), 127800), ((9, 7), 122738), ((9, 14), 124000), ((9, 34), 124402)])
    assert "ENTRY" not in kinds(ev)
    assert e.gap_entry[-1] == pytest.approx(122738 * 0.99875)
    ev = ticks(e, NOV, [((10, 5), 122500)])
    assert kinds(ev) == ["ENTRY"] and e.pos.entry == pytest.approx(122738 * 0.99875)


def test_restart_same_day_restores_state_without_duplicate_trade():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])          # bought once
    saved = e.save_state()
    e2 = engine()
    e2.load_state(saved)
    e2.prepare_day(DAY, EXP, {NOV: HIST})
    assert e2.restore_intraday(saved["intraday"]) == "2026-11-13T10:00:00"
    assert e2.entries_today[1] == 1 and e2.day_open[NOV] == 129000
    assert e2.pos and e2.pos.side == 1


# ---------------- Excel timing, flip and 2nd-session rules ----------------

def test_no_entry_before_0910_order_time():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 129000), ((9, 5), 130300), ((9, 10), 129900)])
    assert "ENTRY" not in kinds(ev)                       # 09:05 cross ignored: orders not placed yet
    ev = ticks(e, NOV, [((9, 40), 129950), ((10, 0), 130200)])
    assert kinds(ev) == ["ENTRY"] and e.pos.entry == pytest.approx(130000 * 1.0012)


def test_crossed_before_0910_follows_gap_rule():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 129800), ((9, 6), 130500), ((9, 10), 130300)])
    assert kinds(ev) == ["GAP"] and e.gap_entry[1] == "wait"   # already beyond 130,156 at 09:10
    ev = ticks(e, NOV, [((9, 16), 130400)])
    assert e.gap_entry[1] == pytest.approx(130500 * 1.00125)   # 15-minute high + 0.125%
    ev = ticks(e, NOV, [((9, 30), 130700)])
    assert kinds(ev) == ["ENTRY"] and e.pos.gap_trade


def test_stop_loss_not_live_before_0910():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])    # long, SL1 ~128,247
    nxt = dt.date(2026, 11, 16)
    e.prepare_day(nxt, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((9, 0), 129000), ((9, 4), 128100), ((9, 10), 128900)]).__iter__()
    assert e.pos is not None                                   # 09:04 dip below SL ignored


def test_flip_stop2_and_sell_entry_trigger_together():
    hist = bars([(130000, 128500), (129800, 128600), (129500, 128700), (129600, 128800)])
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: hist})
    ticks(e, NOV, [((9, 0), 129000), ((9, 10), 129000), ((10, 0), 130200)])    # buy @130,156
    ticks(e, NOV, [((11, 0), 132200)])                                          # target, SL2 = sell entry
    sell = 128500 * 0.9988
    assert e.pos.lots == 1 and e.pos.sl == pytest.approx(sell)
    ev = ticks(e, NOV, [((13, 0), 128000)])
    assert kinds(ev) == ["STOP", "ENTRY"]                     # long out, short in (Excel 19/11 example)
    assert e.pos.side == -1 and e.pos.entry == pytest.approx(sell) and e.pos.lots == 2


def test_second_session_only_day_uses_1700_timing():
    e = engine()
    e.prepare_day(DAY, EXP, {NOV: HIST})
    ev = ticks(e, NOV, [((17, 0), 129000), ((17, 5), 130300)])
    assert e.open_time == dt.time(17, 0) and "ENTRY" not in kinds(ev)
    ev = ticks(e, NOV, [((17, 10), 129900), ((18, 0), 130200)])
    assert kinds(ev) == ["ENTRY"]
