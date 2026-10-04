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

Strategy (Oct 2026 settings — backtested in TradingView and on 2012–2022 data)
  Pairs     : GBP/USD, EUR/USD      Timeframe : 1H candles
  Settings  : ATR 13, Factor 4.5, RR 1.5, no filters  (strategy_settings.json)
  Costs     : 1.5 pips per trade deducted from every result
  Capital   : $300 paper account, 0.01 lot ($0.10 per pip)
  Same numbers as tradingview/supertrend_paper_bot.pine and
  tradingview/supertrend_strategy.pine — keep all three in sync.

Learning (learner.py)
  Every trade stores WHY it was taken (session, ADX, EMA side, stop size) and
  HOW it went (best / worst point before the exit). Monthly — or on /learn —
  the bot sends lessons from that journal and re-tests the strategy on ~2.5
  years of candles. It proposes a settings change only when the change wins in
  both halves of that history; /approve applies it (AUTO_APPLY skips the ask).

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

import learner as L

# =============================================================
#  CONFIG — secrets come from GitHub Actions secrets (env vars)
# =============================================================
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "")
CHAT_ID         = os.environ.get("CHAT_ID",         "")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")

# Strategy settings (ATR, factor, RR, filters) live in strategy_settings.json
# so the learning review can update them after /approve. These module values
# are refreshed from that file at the start of every run (see apply_settings).
S             = dict(L.DEFAULTS)
LEARN_META    = {"pending": None, "history": [], "last_review": None}
ATR_PERIOD    = S["atr_period"]
ST_FACTOR     = S["factor"]
RR            = S["rr"]

PAPER_CAPITAL = 300.0
LOT_SIZE      = 0.01
PIP_USD       = 0.10      # $ per pip at 0.01 lot on a USD-quoted pair
SPREAD_PIPS   = 1.5       # cost per trade (spread + slippage), same as TradingView
INR_PER_USD   = 88.0      # only for showing rupee amounts (approximate)

# Learning: False = proposals wait for /approve.  True = applied automatically.
AUTO_APPLY    = False
HISTORY_PAGES = 3         # x 5,000 hourly candles (~2.5 years) for the monthly re-test

PAIRS = {
    "GBP/USD": "GBP/USD",
    "EUR/USD": "EUR/USD",
}

# Candles fetched per run. Supertrend, ADX and EMA depend on the whole price
# path, so a long history keeps the bot's values identical to TradingView's.
CANDLES = 1000

def apply_settings(s):
    global S, ATR_PERIOD, ST_FACTOR, RR
    S = dict(s)
    ATR_PERIOD, ST_FACTOR, RR = S["atr_period"], S["factor"], S["rr"]

def inr(usd):
    return f"₹{usd * INR_PER_USD:,.0f}"

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
    elif cmd == "/learn":           learning_review(d)
    elif cmd == "/settings":        send_msg(cmd_settings())
    elif cmd == "/approve":         send_msg(cmd_approve())
    elif cmd == "/reject":          send_msg(cmd_reject())
    elif cmd in ("/guide",   "/g"): send_guide()
    elif cmd in ("/help",    "/h", "/start"): send_msg(cmd_help())
    else:                           send_msg("❓ Unknown command. Send /help")

# Bump this when the guide text changes: the bot then sends the new guide once.
GUIDE_VERSION = 2

def guide_parts():
    """The full bot guide, split into Telegram-sized messages (HTML mode)."""
    sp = f"{SPREAD_PIPS}"
    part1 = (
        "📘 <b>SUPERTREND PAPER BOT — GUIDE (1/2)</b>\n\n"
        "<b>What it does</b>\n"
        f"Paper-trades {' and '.join(PAIRS)} on 1-hour candles and reports here. No real money. "
        "Runs free on GitHub Actions, checking about every 5 minutes.\n\n"
        "<b>Settings</b>\n"
        f"• Pairs / timeframe : {', '.join(PAIRS)} — 1H\n"
        f"• Supertrend : ATR {ATR_PERIOD}, factor {ST_FACTOR}\n"
        f"• Stop loss : at the Supertrend line\n"
        f"• Target : {RR}× the stop distance\n"
        f"• Filters : {'ADX on' if S['use_adx_filter'] else 'ADX off'}, "
        f"{'EMA200 on' if S['use_ema_filter'] else 'EMA200 off'}\n"
        f"• Size : {LOT_SIZE} lot (about ₹{PIP_USD * INR_PER_USD:.1f} per pip)\n"
        f"• Paper account : ${PAPER_CAPITAL:.0f} (about {inr(PAPER_CAPITAL)})\n"
        f"• Cost : {sp} pips taken off every result\n"
        "• One trade at a time per pair\n\n"
        "<b>Signal rule</b>\n"
        "When the Supertrend changes colour on a closed 1H candle: green = BUY, red = SELL, "
        "entered at the live price. A flip while that pair has a trade open is ignored. About 3 trades a month per pair.\n\n"
        "<b>Automatic messages (IST)</b>\n"
        "• Signal — entry, stop, target, risk and reward in ₹, flip candle time to match on TradingView\n"
        "• Close — target/stop hit, P&amp;L in $ and ₹, result in R, journal note\n"
        "• Hourly status 1 pm – 11 pm when there is no signal\n"
        "• End-of-day report ~10:30 pm (weekdays)\n"
        "• Weekly close Sat ~2:30 am · Market open Mon ~6:30 am\n"
        "• Monthly report + learning review — last trading day ~10:35 pm"
    )
    part2 = (
        "📘 <b>SUPERTREND PAPER BOT — GUIDE (2/2)</b>\n\n"
        "<b>Commands</b> (replies come on the next run, within a few minutes)\n"
        "/status or /s — bot alive, settings, Supertrend colour, price\n"
        "/balance or /b — balance, P&amp;L in $ and ₹, win rate\n"
        "/trades or /t — open trade, distance to stop and target\n"
        "/log or /l — last 5 closed trades\n"
        "/month or /m — this month's summary\n"
        "/learn — lessons from the trades + settings re-test (about a minute)\n"
        "/settings — active settings and any pending proposal\n"
        "/approve — apply the pending proposal\n"
        "/reject — keep the current settings\n"
        "/guide or /g — this guide\n"
        "/help or /h — short command list\n\n"
        "<b>How the bot learns</b>\n"
        "1. Every trade is journalled: why it was taken (session, trend strength, with/against "
        "EMA200, stop size) and how it went (best and worst point, hours held).\n"
        "2. Monthly (or /learn) it sends lessons grouped by those factors, and re-tests factor "
        "4.0–5.0, RR 1.3–1.7 and the ADX / EMA filters on ~2.5 years of candles.\n"
        "3. It proposes a change only if it beats the current settings in BOTH halves of that "
        "history, on 40+ trades, with nearby settings also profitable.\n"
        "4. You decide: /approve or /reject. After a change, put the same numbers into both "
        "TradingView scripts.\n\n"
        "<b>Good to know</b>\n"
        "• Judge it after 3 months on profit factor (/balance, /month), not single trades.\n"
        "• History, 2012–2022 after costs: GBP/USD profit factor ~1.2; EUR/USD ~0.9 (lost in 6 of "
        "11 years). The review shows each pair separately and flags a pair that keeps losing.\n"
        "• Full trade records: paper_trades.json in your GitHub repo."
    )
    return [part1, part2]

def send_guide():
    for part in guide_parts():
        send_msg(part)

def cmd_help():
    return (
        "🤖 <b>SUPERTREND BOT — Commands</b>\n\n"
        "/status  (or /s)  —  Bot alive + ST direction\n"
        "/balance (or /b)  —  Paper account balance\n"
        "/trades  (or /t)  —  Open trades + distances\n"
        "/log     (or /l)  —  Last 5 closed trades\n"
        "/month   (or /m)  —  This month's summary\n"
        "/learn            —  Lessons from the trades + settings re-test\n"
        "/settings         —  Active settings and any pending proposal\n"
        "/approve          —  Apply the pending proposal\n"
        "/reject           —  Discard the pending proposal\n"
        "/guide   (or /g)  —  Full bot guide\n"
        "/help    (or /h)  —  This message\n\n"
        "<i>Runs on GitHub Actions: checks about every 5 minutes, "
        "so replies can take a few minutes.</i>"
    )

def cmd_status(d):
    now = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
    lines = [
        "✅ <b>BOT IS ALIVE</b>\n",
        f"<b>Time</b>   :  {now}",
        f"<b>Pairs</b>  :  {' + '.join(PAIRS)}",
        f"<b>Setup</b>  :  {L.describe(S)}",
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
        f"<b>Starting Capital</b>  :  ${PAPER_CAPITAL}  (≈{inr(PAPER_CAPITAL)})\n"
        f"<b>Current Balance</b>   :  ${round(cap,  2)}  (≈{inr(cap)})\n"
        f"<b>Total P&amp;L</b>         :  {s1}{round(pnl, 2)} USD  (≈{inr(pnl)})\n"
        f"<b>Total Return</b>      :  {s2}{ret}%\n"
        f"<i>P&amp;L is after a {SPREAD_PIPS}-pip spread per trade.</i>\n\n"
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
def get_data(symbol, outputsize=CANDLES, end_date=None):
    url    = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   "1h",
        "outputsize": outputsize,
        "timezone":   "UTC",       # candle timestamps in UTC (exit logic relies on it)
        "apikey":     TWELVE_DATA_KEY,
        "format":     "JSON"
    }
    if end_date:
        params["end_date"] = end_date
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

def get_history(symbol, pages=HISTORY_PAGES):
    """About pages x 5,000 hourly candles, oldest first (for the learning re-test)."""
    frames, end = [], None
    for _ in range(pages):
        df = get_data(symbol, outputsize=5000, end_date=end)
        if df.empty:
            break
        frames.append(df)
        end = (df.index[0] - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(10)             # free plan allows 8 requests a minute
    if not frames:
        return pd.DataFrame()
    h = pd.concat(frames).sort_index()
    return h[~h.index.duplicated(keep="last")]

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
    """Adds st / dir / atr / adx / ema columns — same maths as TradingView (see learner.py)."""
    if df.empty:
        return df
    try:
        return L.add_indicators(df, S)
    except Exception as e:
        print(f"  [Indicator Error] {e}")
        return df

def detect_signal(df):
    """Flip on the last CLOSED candle (df.iloc[-2]) that passes the active filters."""
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
            if L.filters_ok(1, close, float(c["adx"]), float(c["ema"]), S):
                return "long",  close, st, close + (close-st)*RR, ct
            print("  [Filtered] long flip skipped by filter")
        if pd_ == -1 and cd_ == 1 and (st - close) > 0:
            if L.filters_ok(-1, close, float(c["adx"]), float(c["ema"]), S):
                return "short", close, st, close - (st-close)*RR, ct
            print("  [Filtered] short flip skipped by filter")
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
        f"💼 Lot:{LOT_SIZE}  💸 Risk:-${loss} ({inr(loss)})  💰 Reward:+${prof} ({inr(prof)})\n"
        f"📊 RR: 1:{RR}   ⚙️ {L.describe(S)}\n"
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
              trade_id=None, hours=None, r_mult=None, mfe_r=None, mae_r=None):
    em   = "✅" if "TP" in result else "❌"
    lb   = "TARGET HIT — PROFIT" if "TP" in result else "STOP HIT — LOSS"
    sign = "+" if pnl >= 0 else ""
    tid  = f" (#{trade_id})" if trade_id else ""
    dur  = f"<b>Duration</b>  :  {hours} hours\n" if hours is not None else ""
    why  = ""
    if mfe_r is not None and mae_r is not None:
        if "TP" in result:
            why = (f"\n📝 <b>Journal</b>: worst point was {mae_r:+.2f}R before the target"
                   + (" — nearly stopped out." if mae_r <= -0.7 else "."))
        else:
            why = (f"\n📝 <b>Journal</b>: best point was {mfe_r:+.2f}R before the stop"
                   + (" — it was well in profit, then reversed." if mfe_r >= 0.8 else
                      " — it never really got going."))
    rtxt = f"  ·  {r_mult:+.2f}R" if r_mult is not None else ""
    return (
        f"{em} <b>TRADE CLOSED — {pair}{tid}</b>\n\n"
        f"<b>Result</b>    :  {lb}\n"
        f"<b>Direction</b> :  {direction.upper()}\n\n"
        f"<b>Entry</b>     :  {round(entry,   5)}\n"
        f"<b>Exit</b>      :  {round(exit_px, 5)}\n"
        f"<b>P&amp;L</b>       :  {sign}{round(pnl,2)} USD ({sign}{inr(pnl).replace('₹-', '-₹')}) "
        f"({sign}{round(pips,1)} pips after spread){rtxt}\n"
        f"{dur}\n"
        f"<b>Balance</b>   :  ${round(bal,2)} (≈{inr(bal)})"
        f"{why}"
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
            learning_review(d)


# =============================================================
#  LEARNING — monthly lessons + guarded re-tuning (see learner.py)
# =============================================================
def _settings_table(s):
    return (f"   ATR period        :  {s['atr_period']}\n"
            f"   Supertrend factor :  {s['factor']}\n"
            f"   Reward : Risk     :  {s['rr']}\n"
            f"   ADX filter        :  {'ON, ' + str(s['adx_min']) + '+' if s['use_adx_filter'] else 'off'}\n"
            f"   EMA filter        :  {'ON, EMA' + str(s['ema_len']) if s['use_ema_filter'] else 'off'}")

def send_long(text, limit=3900):
    """Telegram caps messages at 4,096 characters — split on line breaks."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            send_msg(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        send_msg(chunk)

def learning_review(d):
    """Lessons from the journal (all pairs) + one re-test across all pairs.
    The settings are shared, so a change must work for every pair together."""
    try:
        histories = {pair: get_history(sym) for pair, sym in PAIRS.items()}
        lines, proposal = L.review(d.get("closed_trades", []), histories, S, SPREAD_PIPS)
    except Exception as e:
        print(f"[Learn Error] {e}")
        send_msg(f"⚠️ Learning review failed: {e}")
        return
    LEARN_META["last_review"] = datetime.now(IST).isoformat(timespec="minutes")
    if proposal:
        LEARN_META["pending"] = proposal
        if AUTO_APPLY:
            lines.append("\n" + apply_pending("auto-applied by monthly review"))
        else:
            lines.append("\nReply /approve to switch, or /reject to keep the current settings.")
    L.save_settings(S, LEARN_META)
    send_long("\n".join(lines))

def apply_pending(reason):
    p = LEARN_META.get("pending")
    if not p:
        return "Nothing to apply."
    old = dict(S)
    apply_settings({**S, **p["settings"]})
    LEARN_META["history"].append({
        "date": datetime.now(IST).strftime("%d/%m/%Y"), "from": old, "to": dict(S),
        "reason": reason, "evidence": p.get("evidence"),
    })
    LEARN_META["pending"] = None
    L.save_settings(S, LEARN_META)
    return ("✅ <b>Settings updated</b>\n" + _settings_table(S) +
            "\n\n⚠️ Update the same numbers in TradingView (indicator and strategy inputs) "
            "so the chart keeps matching the bot.")

def cmd_settings():
    out = ["⚙️ <b>ACTIVE SETTINGS</b>\n", _settings_table(S),
           f"   Spread / trade    :  {SPREAD_PIPS} pips",
           f"   Auto-apply        :  {'ON' if AUTO_APPLY else 'off (needs /approve)'}"]
    p = LEARN_META.get("pending")
    if p:
        out.append(f"\n💡 <b>Pending proposal</b> ({p['created'][:10]}):\n" + _settings_table(p["settings"]))
        out.append("Reply /approve or /reject.")
    if LEARN_META.get("history"):
        h = LEARN_META["history"][-1]
        out.append(f"\nLast change: {h['date']} — {h['reason']}")
    if LEARN_META.get("last_review"):
        out.append(f"Last review: {LEARN_META['last_review'][:16].replace('T', ' ')} IST")
    return "\n".join(out)

def cmd_approve():
    if not LEARN_META.get("pending"):
        return "Nothing pending. Send /learn to run a review."
    return apply_pending("approved by you via /approve")

def cmd_reject():
    if not LEARN_META.get("pending"):
        return "Nothing pending."
    LEARN_META["pending"] = None
    L.save_settings(S, LEARN_META)
    return "👍 Proposal discarded. Settings unchanged:\n" + _settings_table(S)


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

    # Track how far price went for / against the trade before the exit
    # (journal data: "was it in profit before the stop?", "nearly stopped?").
    risk = abs(t["entry"] - t["sl"]) or 1e-9
    side = 1 if t["direction"] == "long" else -1
    best = worst = 0.0
    for n, (hi, lo) in enumerate(ranges, start=1):
        res = _hit(t, hi, lo)
        fav = (hi - t["entry"]) if side == 1 else (t["entry"] - lo)
        adv = (lo - t["entry"]) if side == 1 else (t["entry"] - hi)
        if res == "SL HIT":
            adv = -risk
            fav = min(fav, (t["tp"] - t["entry"]) * side)
        elif res == "TP HIT":
            fav = (t["tp"] - t["entry"]) * side
        best, worst = max(best, fav / risk), min(worst, adv / risk)
        if res:
            return res, round(best, 2), round(max(worst, -1.0), 2), n
    return None, round(best, 2), round(max(worst, -1.0), 2), len(ranges)

def check_exits(d, dfs):
    for pair, t in list(d.get("open_trades", {}).items()):
        df = dfs.get(pair)
        if df is None or df.empty:
            continue
        try:
            res, mfe_r, mae_r, bars = find_exit(t, df)
            if not res:
                continue
            ep    = t["tp"] if res == "TP HIT" else t["sl"]
            gross = (ep-t["entry"])*10000 if t["direction"] == "long" \
                    else (t["entry"]-ep)*10000
            pips  = gross - SPREAD_PIPS                   # same cost as TradingView
            pnl   = round(pips*PIP_USD, 2)
            r_mult = round(pips / t["sl_pips"], 2) if t.get("sl_pips") else None
            d["capital"]   = d.get("capital",   PAPER_CAPITAL) + pnl
            d["total_pnl"] = d.get("total_pnl", 0.0)          + pnl
            now_ist = datetime.now(IST)
            hours = round((now_ist - datetime.fromisoformat(t["opened_at"])
                           ).total_seconds() / 3600, 2)
            d.setdefault("closed_trades", []).append({
                **t, "exit_price": ep, "pnl_usd": pnl,
                "pnl_pips": round(pips, 1), "result": res,
                "r_multiple": r_mult, "mfe_r": mfe_r, "mae_r": mae_r,
                "bars_held": bars,
                "duration_hours": hours,
                "balance_after": round(d["capital"], 2),
                "closed_at": now_ist.isoformat()
            })
            del d["open_trades"][pair]
            send_msg(close_msg(pair, t["direction"], t["entry"],
                               ep, pnl, pips, res, d["capital"],
                               trade_id=t.get("id"), hours=hours,
                               r_mult=r_mult, mfe_r=mfe_r, mae_r=mae_r))
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

        try:
            features = L.entry_features(df, len(df) - 2, 1 if sig == "long" else -1)
        except Exception as e:
            print(f"  [Features Error] {e}")
            features = None

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
            "opened_at": datetime.now(IST).isoformat(),
            # Journal: why this trade was taken (used by the learning review)
            "features": features,
            "settings": dict(S),
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

    # Send the guide once after each guide update (recorded in paper_trades.json)
    if meta.get("guide_version") != GUIDE_VERSION:
        send_guide()
        meta["guide_version"] = GUIDE_VERSION

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

    s, lmeta = L.load_settings()
    apply_settings(s)
    LEARN_META.update(lmeta)
    print(f"[Settings] {L.describe(S)}")

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
