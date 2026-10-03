"""
SUPERTREND PAPER TRADING BOT — GITHUB ACTIONS EDITION

How it runs
  GitHub Actions wakes this script roughly every 5 minutes (see
  .github/workflows/bot.yml). Each run does ONE pass and exits:
    1. loads state from paper_trades.json
    2. answers any Telegram commands received since the last run
    3. fetches 1H candles, checks exits, looks for a new Supertrend flip
    4. sends scheduled reports (hourly status, EOD, weekly, monthly)
    5. saves state back to paper_trades.json (the workflow commits it)

Strategy (unchanged)
  Pairs     : EUR/USD, GBP/USD      Timeframe : 1H candles
  Settings  : ATR 13, Factor 4.11, RR 1.7
  Capital   : $300 paper account, 0.01 lot ($0.10 per pip)

What changed vs the Railway version
  - No endless loop / scheduler: one pass per run, state lives in the repo
  - Stop-loss / target are checked against each candle's HIGH and LOW since
    entry, so a late or skipped run can no longer miss a hit
  - Scheduled reports fire in a time window (not at an exact minute) because
    GitHub can start runs several minutes late
  - Telegram commands are answered on the next run (up to a few minutes)
"""

import os
import sys
import json
import time
import calendar
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta

# =============================================================
#  CONFIG — secrets come from GitHub Actions secrets (env vars)
# =============================================================
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "")
CHAT_ID         = os.environ.get("CHAT_ID",         "")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")

ATR_PERIOD    = 13
ST_FACTOR     = 4.11
RR            = 1.7
PAPER_CAPITAL = 300.0
LOT_SIZE      = 0.01
PIP_USD       = 0.10      # $ per pip at 0.01 lot on a USD-quoted pair

PAIRS = {
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
}

IST          = timezone(timedelta(hours=5, minutes=30))
NO_SIG_START = 13          # hourly status window (IST hours)
NO_SIG_END   = 23

STATE_FILE = Path(__file__).with_name("paper_trades.json")

# Rebuilt on every run from fresh candles
price_cache = {}


# =============================================================
#  STATE  (paper_trades.json, committed back to the repo)
# =============================================================
def load():
    d = {
        "capital":       PAPER_CAPITAL,
        "open_trades":   {},
        "closed_trades": [],
        "total_pnl":     0.0,
        "meta":          {},
    }
    if STATE_FILE.exists():
        # A corrupt file raises on purpose: failing loudly is better than
        # silently wiping the trade history.
        saved = json.loads(STATE_FILE.read_text())
        if isinstance(saved, dict):
            d.update(saved)
    if not isinstance(d.get("meta"), dict):
        d["meta"] = {}
    return d

def save(d):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, STATE_FILE)
    print(f"[Save] Capital:{round(d.get('capital', 0), 2)} "
          f"Open:{len(d.get('open_trades', {}))} "
          f"Closed:{len(d.get('closed_trades', []))}")


# =============================================================
#  TELEGRAM
# =============================================================
def send_msg(msg: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10)
        if r.status_code == 200:
            print("[TG] Sent.")
        else:
            print(f"[TG Error] {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[TG Exception] {e}")

def fetch_commands(meta):
    """Return the list of commands sent to the bot since the last run."""
    last_id = meta.get("last_update_id", 0)
    params  = {"timeout": 0}
    if last_id:
        params["offset"] = last_id + 1
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params, timeout=15)
        if r.status_code != 200:
            print(f"[GetUpdates Error] {r.status_code} {r.text[:200]}")
            return []
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[GetUpdates Error] {e}")
        return []

    commands = []
    for u in updates:
        meta["last_update_id"] = max(meta.get("last_update_id", 0), u["update_id"])
        msg = u.get("message", {}) or {}
        if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/"):
            commands.append(text.split()[0].split("@")[0])   # "/status@mybot" -> "/status"
    return commands


# =============================================================
#  COMMAND REPLIES
# =============================================================
DATA_CMDS = {"/status", "/s", "/trades", "/t"}

def reply_to(cmd, d):
    if   cmd in ("/status",  "/s"): send_msg(cmd_status(d))
    elif cmd in ("/balance", "/b"): send_msg(cmd_balance(d))
    elif cmd in ("/trades",  "/t"): send_msg(cmd_trades(d))
    elif cmd in ("/month",   "/m"): monthly_report(d)
    elif cmd in ("/log",     "/l"): send_msg(cmd_log(d))
    elif cmd in ("/help",    "/h", "/start"): send_msg(cmd_help())
    else:                           send_msg("❓ Unknown command. Send /help")

def cmd_help():
    return (
        "🤖 <b>SUPERTREND BOT — Commands</b>\n\n"
        "/status  (or /s)  —  Bot alive + ST direction\n"
        "/balance (or /b)  —  Paper account balance\n"
        "/trades  (or /t)  —  Open trades + distances\n"
        "/log     (or /l)  —  Last 5 closed trades\n"
        "/month   (or /m)  —  This month's summary\n"
        "/help    (or /h)  —  This message\n\n"
        "<i>Runs on GitHub Actions: checks about every 5 minutes, "
        "so replies can take a few minutes.</i>"
    )

def cmd_status(d):
    now = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
    lines = [
        "✅ <b>BOT IS ALIVE</b>\n",
        f"<b>Time</b>   :  {now}",
        f"<b>Pairs</b>  :  EUR/USD + GBP/USD",
        f"<b>Check</b>  :  About every 5 minutes\n",
        "─── SUPERTREND STATUS ───"
    ]
    for pair in PAIRS:
        c = price_cache.get(pair)
        if not c:
            lines.append(f"\n⚪ <b>{pair}</b>  —  No data (market closed?)")
        else:
            em = "🟢" if c["color"] == "GREEN" else "🔴"
            tr = "GREEN — Uptrend" if c["color"] == "GREEN" else "RED — Downtrend"
            lines.append(
                f"\n{em} <b>{pair}</b>\n"
                f"   Supertrend :  {tr}\n"
                f"   ST Value   :  {round(c['st'],    5)}\n"
                f"   Price Now  :  {round(c['price'], 5)}"
            )
    lines.append(f"\n<b>Open trades</b>  :  {len(d.get('open_trades', {}))}")
    lines.append("\n<i>Running on GitHub Actions</i>")
    return "\n".join(lines)

def cmd_balance(d):
    cap   = d.get("capital",       PAPER_CAPITAL)
    pnl   = d.get("total_pnl",     0.0)
    ct    = d.get("closed_trades",  [])
    wins  = len([t for t in ct if "TP" in t.get("result", "")])
    total = len(ct)
    wr    = round(wins / total * 100, 1) if total > 0 else 0
    ret   = round(((cap - PAPER_CAPITAL) / PAPER_CAPITAL) * 100, 2)
    s1    = "+" if pnl >= 0 else ""
    s2    = "+" if ret >= 0 else ""
    return (
        "💰 <b>PAPER ACCOUNT BALANCE</b>\n\n"
        f"<b>Starting Capital</b>  :  ${PAPER_CAPITAL}\n"
        f"<b>Current Balance</b>   :  ${round(cap,  2)}\n"
        f"<b>Total P&L</b>         :  {s1}{round(pnl, 2)} USD\n"
        f"<b>Total Return</b>      :  {s2}{ret}%\n\n"
        f"<b>Trades Closed</b>     :  {total}\n"
        f"<b>Wins / Losses</b>     :  {wins} / {total - wins}\n"
        f"<b>Win Rate</b>          :  {wr}%\n\n"
        f"<i>Paper trading only — no real money.</i>"
    )

def cmd_log(d):
    ct = d.get("closed_trades", [])
    if not ct:
        return "📒 <b>TRADE LOG</b>\n\nNo closed trades yet."
    recent = ct[-5:][::-1]
    lines = [f"📒 <b>TRADE LOG — last {len(recent)} of {len(ct)}</b>\n"]
    for t in recent:
        e = "✅" if "TP" in t.get("result", "") else "❌"
        sg = "+" if t.get("pnl_usd", 0) >= 0 else ""
        when = t.get("closed_at", "")[:16].replace("T", " ")
        lines.append(
            f"{e} #{t.get('id', '-')} {t.get('pair', '?')} {str(t.get('direction', '?')).upper()}  "
            f"{sg}{t.get('pnl_usd', 0)} USD ({sg}{t.get('pnl_pips', 0)} pips)\n"
            f"     {when} IST")
    lines.append("\n<i>Full history: paper_trades.json in your GitHub repo.</i>")
    return "\n".join(lines)

def cmd_trades(d):
    trades = d.get("open_trades", {})
    if not trades:
        return (
            "📂 <b>OPEN TRADES</b>\n\n"
            "No open positions right now.\n\n"
            "<i>Bot is watching for signals about every 5 minutes.</i>"
        )
    lines = ["📂 <b>OPEN TRADES</b>\n"]
    for pair, t in trades.items():
        c = price_cache.get(pair)
        if not c:
            sym = PAIRS.get(pair, "")
            lp  = get_live_price(sym) if sym else None
            if lp:
                c = {"price": lp, "st": 0, "color": "UNKNOWN"}
        if c and c.get("price"):
            price   = c["price"]
            dist_sl = round(abs(price - t["sl"]) * 10000, 1)
            dist_tp = round(abs(price - t["tp"]) * 10000, 1)
            pnl_p   = (price - t["entry"]) * 10000 if t["direction"] == "long" \
                      else (t["entry"] - price) * 10000
            pnl_u   = round(pnl_p * PIP_USD, 2)
            sign    = "+" if pnl_u >= 0 else ""
            em      = "🟢" if t["direction"] == "long" else "🔴"
            st_line = ""
            if c.get("color") and c["color"] != "UNKNOWN":
                st_icon = "🟢" if c["color"] == "GREEN" else "🔴"
                st_line = f"  Supertrend :  {st_icon} {c['color']}\n"
            lines.append(
                f"{em} <b>{pair}</b> — {t['direction'].upper()}\n"
                f"  Entry      :  {round(t['entry'], 5)}\n"
                f"  Current    :  {round(price,      5)}\n"
                f"  Dist to SL :  {dist_sl} pips\n"
                f"  Dist to TP :  {dist_tp} pips\n"
                f"{st_line}"
                f"  Unrealised :  {sign}{pnl_u} USD\n"
            )
        else:
            em = "🟢" if t["direction"] == "long" else "🔴"
            lines.append(
                f"{em} <b>{pair}</b> — {t['direction'].upper()}\n"
                f"  Entry      :  {round(t['entry'],5)}\n"
                f"  Stop Loss  :  {round(t['sl'],   5)}\n"
                f"  Target     :  {round(t['tp'],   5)}\n"
                f"  Current    :  Unavailable\n"
            )
    return "\n".join(lines)


# =============================================================
#  DATA FETCH
# =============================================================
def get_data(symbol):
    url    = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   "1h",
        "outputsize": 100,
        "timezone":   "UTC",       # candle timestamps in UTC (exit logic relies on it)
        "apikey":     TWELVE_DATA_KEY,
        "format":     "JSON"
    }
    for attempt in range(1, 4):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code != 200:
                time.sleep(5); continue
            data = r.json()
            if data.get("status") == "error":
                print(f"  [API Error] {data.get('message')}")
                time.sleep(5); continue
            values = data.get("values", [])
            if not values:
                time.sleep(5); continue
            df = pd.DataFrame(values)
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["datetime"] = pd.to_datetime(df["datetime"])
            df.set_index("datetime", inplace=True)
            df = df.iloc[::-1].copy()
            df.dropna(inplace=True)
            if len(df) < 20:
                time.sleep(5); continue
            return df
        except Exception as e:
            print(f"  [Fetch Error {attempt}] {e}")
            time.sleep(5)
    return pd.DataFrame()

def get_live_price(symbol):
    """Latest price right now (not candle close)."""
    url    = "https://api.twelvedata.com/price"
    params = {"symbol": symbol, "apikey": TWELVE_DATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if "price" in data:
                return float(data["price"])
    except Exception as e:
        print(f"  [Live Price Error] {e}")
    return None

def calc_supertrend(df):
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr  = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
        hl2 = (h + l) / 2
        bu  = hl2 + ST_FACTOR * atr
        bl  = hl2 - ST_FACTOR * atr
        fu  = bu.copy()
        fl  = bl.copy()
        for i in range(1, len(df)):
            fu.iloc[i] = bu.iloc[i] if bu.iloc[i] < fu.iloc[i-1] \
                         or c.iloc[i-1] > fu.iloc[i-1] else fu.iloc[i-1]
            fl.iloc[i] = bl.iloc[i] if bl.iloc[i] > fl.iloc[i-1] \
                         or c.iloc[i-1] < fl.iloc[i-1] else fl.iloc[i-1]
        direction  = pd.Series(1.0, index=df.index)
        supertrend = pd.Series(fu.iloc[0], index=df.index)
        for i in range(1, len(df)):
            if direction.iloc[i-1] == 1:
                if c.iloc[i] > fu.iloc[i]:
                    direction.iloc[i]  = -1
                    supertrend.iloc[i] = fl.iloc[i]
                else:
                    direction.iloc[i]  = 1
                    supertrend.iloc[i] = fu.iloc[i]
            else:
                if c.iloc[i] < fl.iloc[i]:
                    direction.iloc[i]  = 1
                    supertrend.iloc[i] = fu.iloc[i]
                else:
                    direction.iloc[i]  = -1
                    supertrend.iloc[i] = fl.iloc[i]
        df        = df.copy()
        df["st"]  = supertrend
        df["dir"] = direction
    except Exception as e:
        print(f"  [ST Error] {e}")
    return df

def detect_signal(df):
    if len(df) < 4 or "dir" not in df.columns:
        return None, None, None, None, None
    try:
        p, c  = df.iloc[-3], df.iloc[-2]
        close = float(c["close"])
        st    = float(c["st"])
        ct    = str(df.index[-2])
        pd_   = float(p["dir"])
        cd_   = float(c["dir"])
        if pd_ == 1 and cd_ == -1 and (close - st) > 0:
            return "long",  close, st, close + (close-st)*RR, ct
        if pd_ == -1 and cd_ == 1 and (st - close) > 0:
            return "short", close, st, close - (st-close)*RR, ct
    except Exception as e:
        print(f"  [Signal Error] {e}")
    return None, None, None, None, None


# =============================================================
#  MESSAGES
# =============================================================
def candle_time_ist(ctime):
    """'2026-10-05 09:00:00' (UTC candle OPEN time) -> '05 Oct 2026  02:30 PM IST'."""
    try:
        dt = datetime.strptime(ctime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%d %b %Y  %I:%M %p IST")
    except Exception:
        return str(ctime)

def sig_msg(pair, direction, live_price, candle_close, sl_from_candle, tp_from_candle,
            ctime=None, trade_id=None):
    """
    Entry = live price right now. SL distance comes from the Supertrend line
    at the flip candle; SL/TP are re-anchored to the live entry price.
    The message also shows the flip candle's time and close so the signal can
    be matched against the TradingView indicator.
    """
    act    = "BUY" if direction == "long" else "SELL"
    em     = "🟢" if direction == "long" else "🔴"
    bar    = "🟩"*10 if direction == "long" else "🟥"*10

    sl_dist = abs(candle_close - sl_from_candle)
    if direction == "long":
        entry  = live_price
        sl     = round(entry - sl_dist, 5)
        tp     = round(entry + sl_dist * RR, 5)
    else:
        entry  = live_price
        sl     = round(entry + sl_dist, 5)
        tp     = round(entry - sl_dist * RR, 5)

    slp    = round(abs(entry - sl) * 10000, 1)
    tpp    = round(abs(tp - entry) * 10000, 1)
    loss   = round(slp * PIP_USD, 2)
    prof   = round(tpp * PIP_USD, 2)
    now    = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
    tid    = f"  #{trade_id}" if trade_id else ""
    chart  = ""
    if ctime:
        chart = (
            f"\n🔎 <b>Match on TradingView (1H)</b>\n"
            f"   Flip candle opened :  {candle_time_ist(ctime)}\n"
            f"   Candle close       :  {round(candle_close, 5)}\n"
        )

    return (
        f"{bar}\n"
        f"‼️ <b>🚨{em}🚨  {act} SIGNAL FIRED{tid}  🚨{em}🚨</b> ‼️\n"
        f"{bar}\n\n"
        f"<b>Pair</b>       :  {pair}\n"
        f"<b>Time</b>       :  {now}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{'⬆️' if direction == 'long' else '⬇️'} ACTION : {act} NOW</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 <b>Entry</b>     :  {round(entry, 5)}  ← live price\n"
        f"🛑 <b>Stop Loss</b> :  {round(sl,    5)}  ({slp} pips)\n"
        f"🎯 <b>Target</b>    :  {round(tp,    5)}  ({tpp} pips)\n\n"
        f"💼 Lot:{LOT_SIZE}  💸 Risk:-${loss}  💰 Reward:+${prof}\n"
        f"📊 RR: 1:{RR}\n"
        f"{chart}\n"
        f"<i>✅ Paper trade logged automatically.</i>\n\n"
        f"{bar}"
    ), entry, sl, tp

def status_msg(statuses):
    now = datetime.now(IST).strftime("%d %b  %I:%M %p IST")
    lines = [f"📋 <b>Status — {now}</b>\n"]
    for pair, color, stv, price in statuses:
        em = "🟢" if color == "GREEN" else "🔴"
        lines.append(
            f"{em} <b>{pair}</b>  —  No signal\n"
            f"   ST:{round(stv,5)}   Price:{round(price,5)}\n"
        )
    lines.append("<i>Checking about every 5 min. /status anytime.</i>")
    return "\n".join(lines)

def close_msg(pair, direction, entry, exit_px, pnl, pips, result, bal,
              trade_id=None, hours=None):
    em   = "✅" if "TP" in result else "❌"
    lb   = "TARGET HIT — PROFIT" if "TP" in result else "STOP HIT — LOSS"
    sign = "+" if pnl >= 0 else ""
    tid  = f" (#{trade_id})" if trade_id else ""
    dur  = f"<b>Duration</b>  :  {hours} hours\n" if hours is not None else ""
    return (
        f"{em} <b>TRADE CLOSED — {pair}{tid}</b>\n\n"
        f"<b>Result</b>    :  {lb}\n"
        f"<b>Direction</b> :  {direction.upper()}\n\n"
        f"<b>Entry</b>     :  {round(entry,   5)}\n"
        f"<b>Exit</b>      :  {round(exit_px, 5)}\n"
        f"<b>P&L</b>       :  {sign}{round(pnl,2)} USD "
        f"({sign}{round(pips,1)} pips)\n"
        f"{dur}\n"
        f"<b>Balance</b>   :  ${round(bal,2)}"
    )

def _open_position_lines(d, extra=""):
    """Shared block listing open positions (EOD / weekly reports)."""
    out = []
    for pair, t in d.get("open_trades", {}).items():
        c = price_cache.get(pair)
        if not c:
            out.append(
                f"\n<b>{pair}</b> — {t['direction'].upper()}\n"
                f"  Entry : {round(t['entry'],5)}  SL : {round(t['sl'],5)}  TP : {round(t['tp'],5)}\n"
                f"  (live price unavailable)"
                + extra
            )
            continue
        price   = c["price"]
        dist_sl = round(abs(price-t["sl"])*10000, 1)
        dist_tp = round(abs(price-t["tp"])*10000, 1)
        pnl_p   = (price-t["entry"])*10000 if t["direction"] == "long" \
                  else (t["entry"]-price)*10000
        pnl_u   = round(pnl_p*PIP_USD, 2)
        sign    = "+" if pnl_u >= 0 else ""
        col     = "🟢 GREEN" if c["color"] == "GREEN" else "🔴 RED"
        out.append(
            f"\n<b>{pair}</b> — {t['direction'].upper()}\n"
            f"  Entry      :  {round(t['entry'],5)}\n"
            f"  Current    :  {round(price,     5)}\n"
            f"  Dist to SL :  {dist_sl} pips\n"
            f"  Dist to TP :  {dist_tp} pips\n"
            f"  Supertrend :  {col}\n"
            f"  Unrealised :  {sign}{pnl_u} USD"
            + extra
        )
    return out

def eod_report(d):
    now = datetime.now(IST)
    ts  = now.strftime("%Y-%m-%d")
    dl  = now.strftime("%d %b %Y")
    ct  = [t for t in d.get("closed_trades", []) if t.get("closed_at", "").startswith(ts)]
    tp_ = sum(t.get("pnl_usd", 0) for t in ct)
    cap = d.get("capital",   PAPER_CAPITAL)
    pnl = d.get("total_pnl", 0.0)
    ret = round(((cap-PAPER_CAPITAL)/PAPER_CAPITAL)*100, 2)
    lines = [f"🌙 <b>EOD REPORT — {dl}</b>\n", "─── OPEN POSITIONS ───"]
    pos = _open_position_lines(d)
    lines.extend(pos if pos else ["No open positions tonight."])
    lines.append("\n─── CLOSED TODAY ───")
    if ct:
        for t in ct:
            s = "+" if t.get("pnl_usd", 0) >= 0 else ""
            e = "✅" if "TP" in t.get("result", "") else "❌"
            lines.append(f"{e} {t['pair']} {t['direction'].upper()} → {s}{t.get('pnl_usd',0)} USD")
    else:
        lines.append("No trades closed today.")
    s1 = "+" if tp_ >= 0 else ""; s2 = "+" if pnl >= 0 else ""; s3 = "+" if ret >= 0 else ""
    lines.append(
        f"\n─── ACCOUNT ───\n"
        f"Start: ${PAPER_CAPITAL}  Now: ${round(cap,2)}\n"
        f"Today: {s1}{round(tp_,2)} USD  Total: {s2}{round(pnl,2)} USD\n"
        f"Return: {s3}{ret}%"
    )
    send_msg("\n".join(lines))

def weekly_close_report(d):
    now = datetime.now(IST)
    dl  = now.strftime("%d %b %Y")
    cap = d.get("capital",   PAPER_CAPITAL)
    pnl = d.get("total_pnl", 0.0)
    ret = round(((cap - PAPER_CAPITAL) / PAPER_CAPITAL) * 100, 2)
    ct  = d.get("closed_trades", [])
    week_ago    = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    week_trades = [t for t in ct if t.get("closed_at", "") >= week_ago]
    week_pnl    = sum(t.get("pnl_usd", 0) for t in week_trades)
    week_wins   = len([t for t in week_trades if "TP" in t.get("result", "")])
    week_total  = len(week_trades)
    week_wr     = round(week_wins / week_total * 100, 1) if week_total > 0 else 0

    lines = [f"📅 <b>WEEKLY CLOSE — {dl}</b>\n"]
    lines.append("─── OPEN POSITIONS (carry over weekend) ───")
    pos = _open_position_lines(d, extra="\n  ⚠️ Gap risk over weekend")
    lines.extend(pos if pos else ["No open positions. Clean into weekend ✅"])

    lines.append("\n─── THIS WEEK ───")
    if week_trades:
        for t in week_trades:
            s = "+" if t.get("pnl_usd", 0) >= 0 else ""
            e = "✅" if "TP" in t.get("result", "") else "❌"
            lines.append(f"{e} {t['pair']} {t['direction'].upper()} → {s}{t.get('pnl_usd',0)} USD")
        s_w = "+" if week_pnl >= 0 else ""
        lines.append(f"\nWeek trades : {week_total}  Wins: {week_wins} ({week_wr}%)  P&L: {s_w}{round(week_pnl,2)} USD")
    else:
        lines.append("No trades closed this week.")

    s1 = "+" if pnl >= 0 else ""; s2 = "+" if ret >= 0 else ""
    lines.append(
        f"\n─── ACCOUNT ───\n"
        f"Balance : ${round(cap,2)}  P&L: {s1}{round(pnl,2)} USD  Return: {s2}{ret}%\n\n"
        f"🌙 Market closed. Reopens Monday 6:30 AM IST."
    )
    send_msg("\n".join(lines))
    print("[Weekly Close Report Sent]")

def weekly_open_message(d):
    now = datetime.now(IST)
    dl  = now.strftime("%d %b %Y")
    cap = d.get("capital", PAPER_CAPITAL)
    open_trades = d.get("open_trades", {})

    lines = [f"🌅 <b>MARKET OPEN — {dl} (Monday)</b>\n", "Forex is live. Bot is watching.\n"]
    if open_trades:
        lines.append("─── CARRIED POSITIONS ───")
        for pair, t in open_trades.items():
            lines.append(
                f"\n<b>{pair}</b> — {t['direction'].upper()}\n"
                f"  Entry : {round(t['entry'],5)}  SL : {round(t['sl'],5)}  TP : {round(t['tp'],5)}\n"
                f"  ⚠️ Check for weekend gaps"
            )
    else:
        lines.append("No carry-over positions. Fresh week ✅")
    lines.append(f"\nBalance : ${round(cap,2)}\n\nSignals active. First status arriving shortly.")
    send_msg("\n".join(lines))
    print("[Weekly Open Message Sent]")

def monthly_report(d):
    now = datetime.now(IST)
    dl  = now.strftime("%b %Y")
    cap = d.get("capital",   PAPER_CAPITAL)
    pnl = d.get("total_pnl", 0.0)
    ret = round(((cap - PAPER_CAPITAL) / PAPER_CAPITAL) * 100, 2)
    ct  = d.get("closed_trades", [])
    month_str    = now.strftime("%Y-%m")
    month_trades = [t for t in ct if t.get("closed_at", "").startswith(month_str)]
    month_pnl    = sum(t.get("pnl_usd", 0) for t in month_trades)
    month_wins   = len([t for t in month_trades if "TP" in t.get("result", "")])
    month_total  = len(month_trades)
    month_wr     = round(month_wins/month_total*100, 1) if month_total > 0 else 0
    gross_profit = sum(t.get("pnl_usd", 0) for t in month_trades if t.get("pnl_usd", 0) > 0)
    gross_loss   = abs(sum(t.get("pnl_usd", 0) for t in month_trades if t.get("pnl_usd", 0) < 0))
    month_pf     = round(gross_profit/gross_loss, 2) if gross_loss > 0 else 0.0

    lines = [f"📊 <b>MONTHLY REPORT — {dl}</b>\n"]
    lines.append("─── THIS MONTH'S TRADES ───")
    if month_trades:
        for t in month_trades:
            s = "+" if t.get("pnl_usd", 0) >= 0 else ""
            e = "✅" if "TP" in t.get("result", "") else "❌"
            d_str = t.get("closed_at", "")[:10]
            lines.append(f"{e} {d_str}  {t['pair']} {t['direction'].upper()} → {s}{t.get('pnl_usd',0)} USD")
    else:
        lines.append("No completed trades this month.")

    s_m = "+" if month_pnl >= 0 else ""
    lines.append(
        f"\n─── MONTH SUMMARY ───\n"
        f"Trades : {month_total}  Wins: {month_wins} ({month_wr}%)  PF: {month_pf}\n"
        f"Month P&L : {s_m}{round(month_pnl,2)} USD"
    )
    s1 = "+" if pnl >= 0 else ""; s2 = "+" if ret >= 0 else ""
    lines.append(
        f"\n─── OVERALL ───\n"
        f"Balance: ${round(cap,2)}  Total P&L: {s1}{round(pnl,2)} USD  Return: {s2}{ret}%"
    )
    send_msg("\n".join(lines))
    print("[Monthly Report Sent]")


# =============================================================
#  SCHEDULED REPORTS — fire in a time WINDOW, once per period
#  (GitHub may start a run late, so exact-minute triggers would be missed)
# =============================================================
def last_trading_day(year, month):
    dt = datetime(year, month, calendar.monthrange(year, month)[1])
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt.date()

def reports_due(meta, now_ist):
    """Names of reports that should be sent now and haven't been yet."""
    today = now_ist.strftime("%Y-%m-%d")
    mins  = now_ist.hour * 60 + now_ist.minute
    wd    = now_ist.weekday()                    # Mon=0 … Sun=6
    due   = []
    # EOD — weekdays from 10:30 PM IST (London close)
    if wd < 5 and mins >= 22*60 + 30 and meta.get("last_eod") != today:
        due.append("eod")
    # Weekly close — Saturday from 2:30 AM IST (Friday 21:00 UTC)
    if wd == 5 and mins >= 2*60 + 30 and meta.get("last_weekly_close") != today:
        due.append("weekly_close")
    # Weekly open — Monday from 6:30 AM IST
    if wd == 0 and mins >= 6*60 + 30 and meta.get("last_weekly_open") != today:
        due.append("weekly_open")
    # Monthly — last trading day of the month from 10:35 PM IST
    month = now_ist.strftime("%Y-%m")
    if (now_ist.date() == last_trading_day(now_ist.year, now_ist.month)
            and mins >= 22*60 + 35 and meta.get("last_monthly") != month):
        due.append("monthly")
    return due

def send_reports(d, due, now_ist):
    meta  = d["meta"]
    today = now_ist.strftime("%Y-%m-%d")
    for name in due:
        if name == "eod":
            eod_report(d);           meta["last_eod"] = today
        elif name == "weekly_close":
            weekly_close_report(d);  meta["last_weekly_close"] = today
        elif name == "weekly_open":
            weekly_open_message(d);  meta["last_weekly_open"] = today
        elif name == "monthly":
            monthly_report(d);       meta["last_monthly"] = now_ist.strftime("%Y-%m")


# =============================================================
#  MARKET HOURS  (forex: Sunday 22:00 UTC → Friday 22:00 UTC)
# =============================================================
def market_open(now_utc):
    wd = now_utc.weekday()
    if wd == 5:                         return False   # Saturday
    if wd == 4 and now_utc.hour >= 22:  return False   # Friday after close
    if wd == 6 and now_utc.hour < 22:   return False   # Sunday before open
    return True


# =============================================================
#  EXIT CHECK — walks every candle since entry (HIGH / LOW)
# =============================================================
def _hit(t, hi, lo):
    """Which level did this price range touch? SL wins if both (conservative)."""
    if t["direction"] == "long":
        hit_sl = lo <= t["sl"]
        hit_tp = hi >= t["tp"]
    else:
        hit_sl = hi >= t["sl"]
        hit_tp = lo <= t["tp"]
    if hit_sl: return "SL HIT"
    if hit_tp: return "TP HIT"
    return None

def find_exit(t, df):
    """
    Candles that started AFTER the entry hour are fully usable (high/low).
    The entry candle itself only counts at its close, because its high/low
    may have happened before we entered.
    """
    opened = datetime.fromisoformat(t["opened_at"]).astimezone(timezone.utc)
    entry_hour = opened.replace(minute=0, second=0, microsecond=0, tzinfo=None)

    ranges = []
    if entry_hour in df.index and entry_hour != df.index[-1]:
        c = float(df.loc[entry_hour, "close"])          # entry candle already closed
        ranges.append((c, c))
    for ts, row in df[df.index > entry_hour].iterrows():
        ranges.append((float(row["high"]), float(row["low"])))
    if entry_hour == df.index[-1]:                       # entry candle still forming
        c = float(df["close"].iloc[-1])
        ranges.append((c, c))

    for hi, lo in ranges:
        res = _hit(t, hi, lo)
        if res:
            return res
    return None

def check_exits(d, dfs):
    for pair, t in list(d.get("open_trades", {}).items()):
        df = dfs.get(pair)
        if df is None or df.empty:
            continue
        try:
            res = find_exit(t, df)
            if not res:
                continue
            ep   = t["tp"] if res == "TP HIT" else t["sl"]
            pips = (ep-t["entry"])*10000 if t["direction"] == "long" \
                   else (t["entry"]-ep)*10000
            pnl  = round(pips*PIP_USD, 2)
            d["capital"]   = d.get("capital",   PAPER_CAPITAL) + pnl
            d["total_pnl"] = d.get("total_pnl", 0.0)          + pnl
            now_ist = datetime.now(IST)
            hours = round((now_ist - datetime.fromisoformat(t["opened_at"])
                           ).total_seconds() / 3600, 2)
            d.setdefault("closed_trades", []).append({
                **t, "exit_price": ep, "pnl_usd": pnl,
                "pnl_pips": round(pips, 1), "result": res,
                "duration_hours": hours,
                "balance_after": round(d["capital"], 2),
                "closed_at": now_ist.isoformat()
            })
            del d["open_trades"][pair]
            send_msg(close_msg(pair, t["direction"], t["entry"],
                               ep, pnl, pips, res, d["capital"],
                               trade_id=t.get("id"), hours=hours))
            print(f"[Closed] {pair} {res} PnL:{pnl}")
        except Exception as e:
            print(f"[Exit Error] {pair}: {e}")


# =============================================================
#  SIGNAL SCAN
# =============================================================
def scan_signals(d, dfs):
    meta     = d["meta"]
    last_sig = meta.setdefault("last_signal", {})
    statuses = []

    for pair, df in dfs.items():
        sym = PAIRS[pair]
        cp  = price_cache[pair]["price"]
        stv = price_cache[pair]["st"]
        col = price_cache[pair]["color"]

        sig, candle_close, candle_sl, candle_tp, ctime = detect_signal(df)

        if sig is None or last_sig.get(pair) == ctime or pair in d.get("open_trades", {}):
            statuses.append((pair, col, stv, cp))
            continue

        live_price = get_live_price(sym)
        if live_price is None:
            live_price = candle_close
            print("  [Warning] Could not get live price, using candle close")

        print(f"  {pair}: *** {sig.upper()} ***")
        print(f"  Candle close: {candle_close:.5f}  Live price: {live_price:.5f}")

        meta["trade_seq"] = meta.get("trade_seq", 0) + 1
        trade_id = meta["trade_seq"]
        msg, entry, sl, tp = sig_msg(
            pair, sig, live_price, candle_close, candle_sl, candle_tp,
            ctime, trade_id)
        sl_pips = round(abs(entry - sl) * 10000, 1)
        tp_pips = round(abs(tp - entry) * 10000, 1)

        # Record the trade BEFORE messaging so a failed send can never
        # cause a duplicate entry on the next run.
        last_sig[pair] = ctime
        d["open_trades"][pair] = {
            "id": trade_id,
            "pair": pair, "direction": sig,
            "entry": entry, "sl": sl, "tp": tp,
            "sl_pips": sl_pips, "tp_pips": tp_pips,
            "risk_usd":   round(sl_pips * PIP_USD, 2),
            "reward_usd": round(tp_pips * PIP_USD, 2),
            "lot": LOT_SIZE,
            "signal_candle_utc": ctime,          # open time of the 1H flip candle
            "candle_close": candle_close,        # TradingView-style entry price
            "opened_at": datetime.now(IST).isoformat()
        }
        send_msg(msg)

    return statuses

def maybe_send_status(meta, statuses, now_ist):
    """Hourly status during trading hours (55-min gap tolerates run jitter)."""
    if not statuses or not (NO_SIG_START <= now_ist.hour < NO_SIG_END):
        return
    last = meta.get("last_status_at")
    if last:
        try:
            mins = (now_ist - datetime.fromisoformat(last)).total_seconds() / 60
            if mins < 55:
                return
        except ValueError:
            pass
    send_msg(status_msg(statuses))
    meta["last_status_at"] = now_ist.isoformat()
    print("  [Status sent]")


# =============================================================
#  ONE RUN
# =============================================================
def run(d, now_utc, now_ist):
    meta     = d["meta"]
    commands = fetch_commands(meta)
    due      = reports_due(meta, now_ist)
    is_open  = market_open(now_utc)

    print(f"[{now_ist.strftime('%d %b %H:%M IST')}] market_open={is_open} "
          f"commands={commands} reports_due={due}")

    need_data = is_open \
        or any(c in DATA_CMDS for c in commands) \
        or (due and d["open_trades"])

    dfs = {}
    price_cache.clear()
    if need_data:
        for pair, sym in PAIRS.items():
            df = calc_supertrend(get_data(sym))
            if df.empty or "st" not in df.columns:
                print(f"  [Skip] {pair}")
                continue
            try:
                price_cache[pair] = {
                    "price": float(df["close"].iloc[-1]),
                    "st":    float(df["st"].iloc[-2]),
                    "color": "GREEN" if float(df["dir"].iloc[-2]) == -1 else "RED",
                }
                dfs[pair] = df
            except Exception as e:
                print(f"  [Cache Error] {pair}: {e}")

    if is_open and dfs:
        check_exits(d, dfs)
        statuses = scan_signals(d, dfs)
        maybe_send_status(meta, statuses, now_ist)

    for cmd in commands:
        reply_to(cmd, d)

    if due:
        send_reports(d, due, now_ist)


def main():
    missing = [n for n, v in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
                              ("CHAT_ID", CHAT_ID),
                              ("TWELVE_DATA_KEY", TWELVE_DATA_KEY)) if not v]
    if missing:
        print(f"ERROR: missing secrets: {', '.join(missing)}")
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    d      = load()
    before = json.dumps(d, sort_keys=True)
    try:
        run(d, now_utc, now_ist)
    finally:
        # Save even if something above crashed, but only when it changed
        # (keeps the repo history free of empty commits).
        if json.dumps(d, sort_keys=True) != before:
            save(d)


if __name__ == "__main__":
    main()
