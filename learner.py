"""
learner.py — trade journal analysis and guarded self-tuning for the Supertrend bot.

The bot calls this module to:
  1. compute the indicators (Supertrend, ADX, EMA) exactly like TradingView
  2. record WHY each trade was taken (context at entry) and HOW it played out
     (best / worst point reached before the exit)
  3. once a month (or on /learn) write a lesson report from the journal and
     re-test the strategy on ~2.5 years of hourly candles

Why the settings never change on a single trade
  The bot makes about 3 trades a month. A few months of paper trades is far too
  little data to tune anything — tuning on it just chases luck. So the journal
  is used for LESSONS (patterns to watch), while setting changes need the long
  re-test to agree:
    - only settings inside a pre-tested safe range (factor 4.0–5.0, RR 1.3–1.7,
      two optional filters) can ever be chosen
    - the new setting must beat the current one in BOTH halves of the history,
      on at least MIN_TRADES trades, and its neighbouring settings must also be
      profitable (so it isn't a lucky spike)
    - the change is applied after you send /approve (or automatically if the
      bot's AUTO_APPLY switch is on)
"""

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from pathlib import Path

SETTINGS_FILE = Path(__file__).with_name("strategy_settings.json")

DEFAULTS = {
    "atr_period":     13,
    "factor":         4.5,
    "rr":             1.5,
    "use_adx_filter": False,   # skip signals when ADX(14) is below adx_min
    "adx_min":        20,
    "use_ema_filter": False,   # only trade in the direction of EMA(ema_len)
    "ema_len":        200,
}

FACTOR_GRID = [4.0, 4.25, 4.5, 4.75, 5.0]
RR_GRID     = [1.3, 1.5, 1.7]
MIN_TRADES  = 40       # back-test trades required before any proposal
MIN_GAIN    = 0.10     # profit-factor gain required in EACH half of the history
MIN_PF      = 1.10     # candidate profit factor required in EACH half
MIN_GROUP   = 5        # journal trades per group before a pattern is mentioned


# =============================================================
#  SETTINGS FILE  (strategy_settings.json, committed by the workflow)
# =============================================================
def load_settings():
    s = dict(DEFAULTS)
    meta = {"pending": None, "history": [], "last_review": None}
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text())
        s.update(saved.get("active", {}))
        for k in meta:
            if k in saved:
                meta[k] = saved[k]
    return s, meta

def save_settings(s, meta):
    data = {"active": s, **meta}
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(SETTINGS_FILE)

def describe(s):
    f = []
    if s["use_adx_filter"]:
        f.append(f"ADX {s['adx_min']}+ only")
    if s["use_ema_filter"]:
        f.append(f"with EMA{s['ema_len']} only")
    return (f"ATR {s['atr_period']} · Factor {s['factor']} · RR 1:{s['rr']}"
            + (" · " + ", ".join(f) if f else " · no filters"))

def same(a, b):
    return all(a[k] == b[k] for k in DEFAULTS)


# =============================================================
#  INDICATORS — same maths as TradingView's ta.supertrend / ta.dmi / ta.ema
# =============================================================
def _rma(x, n):
    return pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().to_numpy()

def supertrend(h, l, c, factor, n):
    """Returns (supertrend line, direction, atr). direction -1 = uptrend, +1 = downtrend."""
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    atr = _rma(tr, n)
    hl2 = (h + l) / 2
    ub, lb = hl2 + factor * atr, hl2 - factor * atr
    N = len(c)
    U, L = ub.copy(), lb.copy()
    D = np.ones(N)
    ST = np.full(N, np.nan)
    if N:
        ST[0] = U[0]
    for i in range(1, N):
        L[i] = lb[i] if (lb[i] > L[i - 1] or c[i - 1] < L[i - 1]) else L[i - 1]
        U[i] = ub[i] if (ub[i] < U[i - 1] or c[i - 1] > U[i - 1]) else U[i - 1]
        if i < n:
            D[i] = 1
        elif ST[i - 1] == U[i - 1]:
            D[i] = -1 if c[i] > U[i] else 1
        else:
            D[i] = 1 if c[i] < L[i] else -1
        ST[i] = L[i] if D[i] == -1 else U[i]
    return ST, D, atr

def adx(h, l, c, n=14):
    up = np.r_[np.nan, np.diff(h)]
    dn = np.r_[np.nan, -np.diff(l)]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    trr = _rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = 100 * _rma(pdm, n) / trr
        m = 100 * _rma(mdm, n) / trr
        s = p + m
        dx = np.nan_to_num(np.abs(p - m) / np.where(s == 0, 1, s))
    return 100 * _rma(dx, n)

def add_indicators(df, s):
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    st, d, atr = supertrend(h, l, c, s["factor"], s["atr_period"])
    out = df.copy()
    out["st"], out["dir"], out["atr"] = st, d, atr
    out["adx"] = adx(h, l, c, 14)
    out["ema"] = pd.Series(c).ewm(span=s["ema_len"], adjust=False).mean().to_numpy()
    return out

def filters_ok(side, close, adx_v, ema_v, s):
    """side: 1 = long, -1 = short."""
    if s["use_adx_filter"] and not (adx_v >= s["adx_min"]):
        return False
    if s["use_ema_filter"] and not ((close > ema_v) if side == 1 else (close < ema_v)):
        return False
    return True


# =============================================================
#  JOURNAL — context at entry, excursions at exit
# =============================================================
def session_of(hour_utc):
    if hour_utc < 7:
        return "Asia"
    if hour_utc < 13:
        return "London"
    if hour_utc < 21:
        return "New York"
    return "Late"

def entry_features(x, i, side, pip=1e-4):
    """x: DataFrame from add_indicators; i: integer position of the flip candle."""
    row = x.iloc[i]
    ts = x.index[i]
    d = x["dir"].to_numpy()
    j = i - 1
    while j > 0 and d[j - 1] == d[i - 1]:
        j -= 1
    stop = abs(float(row["close"]) - float(row["st"]))
    return {
        "hour_utc":        int(ts.hour),
        "session":         session_of(ts.hour),
        "weekday":         ts.strftime("%a"),
        "adx":             round(float(row["adx"]), 1),
        "ema_side":        "with" if (float(row["close"]) > float(row["ema"])) == (side == 1) else "against",
        "atr_pips":        round(float(row["atr"]) / pip, 1),
        "stop_pips":       round(stop / pip, 1),
        "stop_atr":        round(stop / float(row["atr"]), 2),
        "prev_trend_bars": int(i - j),
    }


# =============================================================
#  BACK-TEST — identical rules to the bot and the TradingView scripts
# =============================================================
def backtest(df, s, spread_pips, pip=1e-4):
    """R-multiple of every trade (after spread), indexed by exit time."""
    x = add_indicators(df, s)
    h, l, c = (x[k].to_numpy(float) for k in ("high", "low", "close"))
    st, d, ad, em = (x[k].to_numpy(float) for k in ("st", "dir", "adx", "ema"))
    R, times = [], []
    i, N = s["atr_period"] + 1, len(c)
    while i < N - 1:
        up = d[i] < 0 and d[i - 1] > 0
        dn = d[i] > 0 and d[i - 1] < 0
        if not (up or dn):
            i += 1
            continue
        side = 1 if up else -1
        dist = abs(c[i] - st[i])
        if dist <= 0 or not filters_ok(side, c[i], ad[i], em[i], s):
            i += 1
            continue
        sl = c[i] - side * dist
        tp = c[i] + side * s["rr"] * dist
        res = None
        for j in range(i + 1, N):
            if (l[j] <= sl) if side == 1 else (h[j] >= sl):
                res = -1.0
                break
            if (h[j] >= tp) if side == 1 else (l[j] <= tp):
                res = float(s["rr"])
                break
        if res is None:          # still open at the end of the data
            break
        R.append(res - spread_pips * pip / dist)
        times.append(x.index[j])
        i = j                    # a flip on the exit candle can start the next trade
    return pd.Series(R, index=pd.DatetimeIndex(times), dtype=float)

def pf(R):
    g, b = R[R > 0].sum(), -R[R < 0].sum()
    if b > 0:
        return float(g / b)
    return 9.99 if g > 0 else 0.0

def retest(df, s, spread_pips):
    mid = df.index[len(df) // 2]

    def score(cfg):
        R = backtest(df, cfg, spread_pips)
        a, b = R[R.index < mid], R[R.index >= mid]
        return {"n": int(len(R)), "pf": pf(R), "pf1": pf(a), "pf2": pf(b),
                "n1": int(len(a)), "n2": int(len(b)), "net_r": round(float(R.sum()), 1)}

    cur = score(s)
    grid = {}
    cands = []
    for f in FACTOR_GRID:
        for rr in RR_GRID:
            cfg = {**s, "factor": f, "rr": rr}
            r = cur if same(cfg, s) else score(cfg)
            grid[(f, rr)] = r
            cands.append((cfg, r, "settings"))
    for ua in (False, True):
        for ue in (False, True):
            cfg = {**s, "use_adx_filter": ua, "use_ema_filter": ue}
            if not same(cfg, s):
                cands.append((cfg, score(cfg), "filters"))

    best = None
    for cfg, r, kind in cands:
        if same(cfg, s) or r["n"] < MIN_TRADES:
            continue
        if r["pf1"] < max(MIN_PF, cur["pf1"] + MIN_GAIN):
            continue
        if r["pf2"] < max(MIN_PF, cur["pf2"] + MIN_GAIN):
            continue
        if kind == "settings":
            k = FACTOR_GRID.index(cfg["factor"])
            nbrs = [FACTOR_GRID[x] for x in (k - 1, k + 1) if 0 <= x < len(FACTOR_GRID)]
            if any(grid[(f, cfg["rr"])]["pf"] < 1.0 for f in nbrs):
                continue
        key = min(r["pf1"], r["pf2"])
        if best is None or key > best[2]:
            best = (cfg, r, key)
    return cur, grid, best


# =============================================================
#  LESSONS FROM THE PAPER-TRADE JOURNAL
# =============================================================
def _group_line(name, g):
    wr = 100 * g["win"].mean()
    return f"   {name:<14} {len(g):>3} trades · {wr:4.0f}% win · {g['R'].sum():+.1f}R"

def journal_lessons(closed_trades):
    rows = []
    for t in closed_trades:
        f = t.get("features")
        if not f or "r_multiple" not in t:
            continue
        rows.append({**f, "R": t["r_multiple"], "win": "TP" in t.get("result", ""),
                     "mfe": t.get("mfe_r"), "mae": t.get("mae_r"),
                     "bars": t.get("bars_held")})
    if not rows:
        return ["No closed trades with entry context yet — the journal fills as trades close."]

    j = pd.DataFrame(rows)
    n = len(j)
    out = [f"<b>Journal</b>: {n} trades · {100 * j['win'].mean():.0f}% win · "
           f"{j['R'].sum():+.1f}R · profit factor {pf(j['R']):.2f}"]

    med_stop = j["stop_pips"].median()
    # Telegram HTML mode rejects bare < and >, so labels use words
    j["stop_size"] = np.where(j["stop_pips"] <= med_stop, f"stop up to {med_stop:.0f}p",
                              f"stop over {med_stop:.0f}p")
    j["trend_strength"] = np.where(j["adx"] >= 20, "ADX 20+", "ADX under 20")
    j["ema"] = np.where(j["ema_side"] == "with", "with EMA200", "against EMA200")

    patterns = []
    for col, title in (("session", "By session"), ("trend_strength", "By trend strength"),
                       ("ema", "By trend direction"), ("stop_size", "By stop size")):
        out.append(f"\n{title}")
        groups = {k: g for k, g in j.groupby(col)}
        for k, g in groups.items():
            out.append(_group_line(k, g))
        big = {k: g for k, g in groups.items() if len(g) >= MIN_GROUP}
        if len(big) >= 2:
            avg = {k: g["R"].mean() for k, g in big.items()}
            hi, lo = max(avg, key=avg.get), min(avg, key=avg.get)
            if avg[hi] - avg[lo] >= 0.5:
                patterns.append(f"• {hi} trades did much better than {lo} trades "
                                f"({avg[hi]:+.2f}R vs {avg[lo]:+.2f}R per trade).")

    losers = j[~j["win"]]
    winners = j[j["win"]]
    if len(losers):
        k = int((losers["mfe"].fillna(0) >= 0.8).sum())
        out.append(f"\nWhy stops were hit\n   {k} of {len(losers)} losing trades were at least +0.8R "
                   f"in profit before reversing.")
        out.append(f"   Losing trades lasted {losers['bars'].mean():.0f} hours on average.")
        if len(losers) >= MIN_GROUP and k / len(losers) >= 0.4:
            patterns.append("• Many losers gave back a good profit first — the trend often stalled "
                            "before the target.")
    if len(winners):
        k = int((winners["mae"].fillna(0) >= 0.7).sum())
        out.append(f"\nWhy targets were hit\n   {k} of {len(winners)} winning trades nearly hit the stop "
                   f"(-0.7R or worse) first.")
        out.append(f"   Winning trades lasted {winners['bars'].mean():.0f} hours on average.")

    out.append("\n<b>Lessons</b>")
    out.extend(patterns or ["• No clear pattern yet."])
    if n < 30:
        out.append(f"<i>Only {n} trades — treat these as hints, not proof. Settings only change "
                   f"when the long re-test agrees.</i>")
    return out


# =============================================================
#  FULL MONTHLY REVIEW
# =============================================================
def review(closed_trades, history_df, s, spread_pips):
    """Returns (report lines, proposal dict or None)."""
    lines = ["🧠 <b>LEARNING REVIEW</b>\n", f"<b>Current</b>: {describe(s)}\n"]
    lines += journal_lessons(closed_trades)

    if history_df is None or len(history_df) < 2000:
        lines.append("\n⚠️ Not enough price history downloaded for the re-test — settings unchanged.")
        return lines, None

    start = history_df.index[0].strftime("%d/%m/%Y")
    end = history_df.index[-1].strftime("%d/%m/%Y")
    cur, grid, best = retest(history_df, s, spread_pips)
    lines.append(f"\n<b>Re-test</b> {start} – {end} (same rules, after spread)")
    lines.append(f"   Current settings: {cur['n']} trades · PF {cur['pf']:.2f} "
                 f"(first half {cur['pf1']:.2f}, second half {cur['pf2']:.2f}) · {cur['net_r']:+.1f}R")
    top = sorted(grid.items(), key=lambda kv: min(kv[1]["pf1"], kv[1]["pf2"]), reverse=True)[:3]
    lines.append("   Steadiest settings in the safe range:")
    for (f, rr), r in top:
        lines.append(f"     factor {f} / RR {rr}: PF {r['pf1']:.2f} → {r['pf2']:.2f} ({r['n']} trades)")

    if best is None:
        lines.append("\n✅ <b>Decision</b>: keep the current settings. Nothing beat them clearly in "
                     "both halves of the history.")
        return lines, None

    cfg, r, _ = best
    proposal = {
        "settings": {k: cfg[k] for k in DEFAULTS},
        "evidence": r,
        "baseline": cur,
        "created": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    }
    lines.append(f"\n💡 <b>Proposal</b>: {describe(cfg)}")
    lines.append(f"   PF {r['pf1']:.2f} → {r['pf2']:.2f} in the two halves vs "
                 f"{cur['pf1']:.2f} → {cur['pf2']:.2f} now ({r['n']} trades).")
    return lines, proposal
