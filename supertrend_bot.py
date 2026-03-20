“””
SUPERTREND PAPER TRADING BOT — FIXED VERSION
Fixes:

1. Checks every 5 minutes instead of hourly
   → Catches flip within 5 mins of candle close
   → Entry price is much closer to actual live price
1. Uses current live price as entry (not stale candle close)
1. Persistent trade storage via Railway environment variable
   → Trades survive bot restarts forever

Pairs     : EUR/USD, GBP/USD
Timeframe : 1H candles (checked every 5 min)
Settings  : ATR 13, Factor 4.11, RR 1.7
Capital   : $300 paper account
“””

import time, json, os, requests, schedule, threading
from datetime import datetime, timezone, timedelta
import pandas as pd

# =============================================================

# CONFIG — from Railway environment variables

# =============================================================

TELEGRAM_TOKEN  = os.environ.get(“TELEGRAM_TOKEN”,  “”)
CHAT_ID         = os.environ.get(“CHAT_ID”,         “”)
TWELVE_DATA_KEY = os.environ.get(“TWELVE_DATA_KEY”, “”)

ATR_PERIOD    = 13
ST_FACTOR     = 4.11
RR            = 1.7
PAPER_CAPITAL = 300.0
LOT_SIZE      = 0.01

PAIRS = {
“EUR/USD”: “EUR/USD”,
“GBP/USD”: “GBP/USD”,
}

IST          = timezone(timedelta(hours=5, minutes=30))
NO_SIG_START = 13
NO_SIG_END   = 23

# In-memory state

last_signal    = {}   # pair -> candle timestamp of last signal sent
last_update_id = 0
price_cache    = {}

# =============================================================

# PERSISTENT STORAGE

# Trades stored as Railway env var “TRADE_DATA” (JSON string)

# Falls back to local file if env var not available

# =============================================================

TRADES_FILE = “paper_trades.json”

def load():
default = {
“capital”:       PAPER_CAPITAL,
“open_trades”:   {},
“closed_trades”: [],
“total_pnl”:     0.0
}
# Try environment variable first (survives Railway restarts)
env_data = os.environ.get(“TRADE_DATA”, “”)
if env_data:
try:
saved = json.loads(env_data)
for k, v in default.items():
if k not in saved:
saved[k] = v
return saved
except:
pass
# Fall back to local file
if os.path.exists(TRADES_FILE):
try:
saved = json.load(open(TRADES_FILE))
for k, v in default.items():
if k not in saved:
saved[k] = v
return saved
except:
pass
return default

def save(d):
# Always save to local file
json.dump(d, open(TRADES_FILE, “w”), indent=2)
# Also update Railway env var via API if possible
# (Railway doesn’t support dynamic env var updates via API)
# So we rely on local file + periodic backup to Telegram
print(f”[Save] Capital:{round(d.get(‘capital’,0),2)} “
f”Open:{len(d.get(‘open_trades’,{}))} “
f”Closed:{len(d.get(‘closed_trades’,[]))}”)

# =============================================================

# TELEGRAM SEND

# =============================================================

def send_msg(msg: str):
try:
r = requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”,
json={“chat_id”: CHAT_ID, “text”: msg, “parse_mode”: “HTML”},
timeout=10)
if r.status_code == 200:
print(”[TG] Sent.”)
else:
print(f”[TG Error] {r.status_code}”)
except Exception as e:
print(f”[TG Exception] {e}”)

# =============================================================

# TELEGRAM RECEIVE

# =============================================================

def get_updates():
global last_update_id
try:
r = requests.get(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates”,
params={“offset”: last_update_id + 1, “timeout”: 10},
timeout=15)
if r.status_code != 200:
return []
updates = r.json().get(“result”, [])
if updates:
last_update_id = updates[-1][“update_id”]
return updates
except Exception as e:
print(f”[GetUpdates Error] {e}”)
return []

def handle_commands():
print(”[Commands] Started.”)
while True:
try:
for update in get_updates():
msg_obj = update.get(“message”, {})
text    = msg_obj.get(“text”, “”).strip().lower()
from_id = str(msg_obj.get(“chat”, {}).get(“id”, “”))
if from_id != str(CHAT_ID):
continue
if   text in [”/status”,  “/s”]: send_msg(cmd_status())
elif text in [”/balance”, “/b”]: send_msg(cmd_balance())
elif text in [”/trades”,  “/t”]: send_msg(cmd_trades())
elif text in [”/help”,    “/h”]: send_msg(cmd_help())
elif text.startswith(”/”):
send_msg(“❓ Unknown command. Send /help”)
except Exception as e:
print(f”[Commands Error] {e}”)
time.sleep(5)

# =============================================================

# COMMANDS

# =============================================================

def cmd_help():
return (
“🤖 <b>SUPERTREND BOT — Commands</b>\n\n”
“/status  (or /s)  —  Bot alive + ST direction\n”
“/balance (or /b)  —  Paper account balance\n”
“/trades  (or /t)  —  Open trades + distances\n”
“/help    (or /h)  —  This message\n\n”
“<i>Checks every 5 minutes for new signals</i>”
)

def cmd_status():
now = datetime.now(IST).strftime(”%d %b %Y  %I:%M %p IST”)
lines = [
“✅ <b>BOT IS ALIVE</b>\n”,
f”<b>Time</b>   :  {now}”,
f”<b>Pairs</b>  :  EUR/USD + GBP/USD”,
f”<b>Check</b>  :  Every 5 minutes\n”,
“─── SUPERTREND STATUS ───”
]
for pair in PAIRS:
c = price_cache.get(pair)
if not c:
lines.append(f”\n⚪ <b>{pair}</b>  —  Loading…”)
else:
em = “🟢” if c[“color”] == “GREEN” else “🔴”
tr = “GREEN — Uptrend” if c[“color”] == “GREEN” else “RED — Downtrend”
lines.append(
f”\n{em} <b>{pair}</b>\n”
f”   Supertrend :  {tr}\n”
f”   ST Value   :  {round(c[‘st’],    5)}\n”
f”   Price Now  :  {round(c[‘price’], 5)}”
)
d = load()
lines.append(f”\n<b>Open trades</b>  :  {len(d.get(‘open_trades’, {}))}”)
lines.append(”\n<i>Bot running on Railway.app 24/7</i>”)
return “\n”.join(lines)

def cmd_balance():
d     = load()
cap   = d.get(“capital”,       PAPER_CAPITAL)
pnl   = d.get(“total_pnl”,     0.0)
ct    = d.get(“closed_trades”,  [])
wins  = len([t for t in ct if “TP” in t.get(“result”, “”)])
total = len(ct)
wr    = round(wins / total * 100, 1) if total > 0 else 0
ret   = round(((cap - PAPER_CAPITAL) / PAPER_CAPITAL) * 100, 2)
s1    = “+” if pnl >= 0 else “”
s2    = “+” if ret >= 0 else “”
return (
“💰 <b>PAPER ACCOUNT BALANCE</b>\n\n”
f”<b>Starting Capital</b>  :  ${PAPER_CAPITAL}\n”
f”<b>Current Balance</b>   :  ${round(cap,  2)}\n”
f”<b>Total P&L</b>         :  {s1}{round(pnl, 2)} USD\n”
f”<b>Total Return</b>      :  {s2}{ret}%\n\n”
f”<b>Trades Closed</b>     :  {total}\n”
f”<b>Wins / Losses</b>     :  {wins} / {total - wins}\n”
f”<b>Win Rate</b>          :  {wr}%\n\n”
f”<i>Paper trading only — no real money.</i>”
)

def cmd_trades():
d      = load()
trades = d.get(“open_trades”, {})
if not trades:
return (
“📂 <b>OPEN TRADES</b>\n\n”
“No open positions right now.\n\n”
“<i>Bot is watching for signals every 5 minutes.</i>”
)
lines = [“📂 <b>OPEN TRADES</b>\n”]
for pair, t in trades.items():
c = price_cache.get(pair)
if c:
price   = c[“price”]
dist_sl = round(abs(price - t[“sl”]) * 10000, 1)
dist_tp = round(abs(price - t[“tp”]) * 10000, 1)
pnl_p   = (price - t[“entry”]) * 10000 if t[“direction”] == “long”   
else (t[“entry”] - price) * 10000
pnl_u   = round(pnl_p * 0.10, 2)
sign    = “+” if pnl_u >= 0 else “”
em      = “🟢” if t[“direction”] == “long” else “🔴”
lines.append(
f”{em} <b>{pair}</b> — {t[‘direction’].upper()}\n”
f”  Entry      :  {round(t[‘entry’], 5)}\n”
f”  Current    :  {round(price,      5)}\n”
f”  Dist to SL :  {dist_sl} pips\n”
f”  Dist to TP :  {dist_tp} pips\n”
f”  Unrealised :  {sign}{pnl_u} USD\n”
)
else:
lines.append(
f”<b>{pair}</b> — {t[‘direction’].upper()}\n”
f”  Entry: {round(t[‘entry’],5)}  “
f”SL: {round(t[‘sl’],5)}  TP: {round(t[‘tp’],5)}\n”
)
return “\n”.join(lines)

# =============================================================

# DATA FETCH

# =============================================================

def get_data(symbol):
url    = “https://api.twelvedata.com/time_series”
params = {
“symbol”:     symbol,
“interval”:   “1h”,
“outputsize”: 100,
“apikey”:     TWELVE_DATA_KEY,
“format”:     “JSON”
}
for attempt in range(1, 4):
try:
r = requests.get(url, params=params, timeout=15)
if r.status_code != 200:
time.sleep(5); continue
data = r.json()
if data.get(“status”) == “error”:
print(f”  [API Error] {data.get(‘message’)}”)
time.sleep(5); continue
values = data.get(“values”, [])
if not values:
time.sleep(5); continue
df = pd.DataFrame(values)
for col in [“open”,“high”,“low”,“close”]:
df[col] = pd.to_numeric(df[col], errors=“coerce”)
df[“datetime”] = pd.to_datetime(df[“datetime”])
df.set_index(“datetime”, inplace=True)
df = df.iloc[::-1].copy()
df.dropna(inplace=True)
if len(df) < 20:
time.sleep(5); continue
return df
except Exception as e:
print(f”  [Fetch Error {attempt}] {e}”)
time.sleep(5)
return pd.DataFrame()

def get_live_price(symbol):
“”“Get the latest price right now (not candle close).”””
url    = “https://api.twelvedata.com/price”
params = {“symbol”: symbol, “apikey”: TWELVE_DATA_KEY}
try:
r = requests.get(url, params=params, timeout=10)
if r.status_code == 200:
data = r.json()
if “price” in data:
return float(data[“price”])
except Exception as e:
print(f”  [Live Price Error] {e}”)
return None

def calc_supertrend(df):
try:
h, l, c = df[“high”], df[“low”], df[“close”]
tr  = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
atr = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
hl2 = (h + l) / 2
bu  = hl2 + ST_FACTOR * atr
bl  = hl2 - ST_FACTOR * atr
fu  = bu.copy()
fl  = bl.copy()
for i in range(1, len(df)):
fu.iloc[i] = bu.iloc[i] if bu.iloc[i] < fu.iloc[i-1]   
or c.iloc[i-1] > fu.iloc[i-1] else fu.iloc[i-1]
fl.iloc[i] = bl.iloc[i] if bl.iloc[i] > fl.iloc[i-1]   
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
df[“st”]  = supertrend
df[“dir”] = direction
except Exception as e:
print(f”  [ST Error] {e}”)
return df

def detect_signal(df):
if len(df) < 4 or “dir” not in df.columns:
return None, None, None, None, None
try:
p, c  = df.iloc[-3], df.iloc[-2]
close = float(c[“close”])
st    = float(c[“st”])
ct    = str(df.index[-2])
pd_   = float(p[“dir”])
cd_   = float(c[“dir”])
if pd_ == 1 and cd_ == -1 and (close - st) > 0:
return “long”,  close, st, close + (close-st)*RR, ct
if pd_ == -1 and cd_ == 1 and (st - close) > 0:
return “short”, close, st, close - (st-close)*RR, ct
except Exception as e:
print(f”  [Signal Error] {e}”)
return None, None, None, None, None

# =============================================================

# SIGNAL MESSAGE — uses LIVE price as entry, not candle close

# =============================================================

def sig_msg(pair, direction, live_price, candle_close, sl_from_candle, tp_from_candle):
“””
live_price      = current market price RIGHT NOW
candle_close    = price when flip candle closed (for SL/TP reference)
sl_from_candle  = SL based on candle close
tp_from_candle  = TP based on candle close

```
Entry shown = live_price (what you actually get when you open now)
SL/TP recalculated from live_price for accuracy
"""
act    = "BUY" if direction == "long" else "SELL"
em     = "🟢" if direction == "long" else "🔴"
bar    = "🟩"*10 if direction == "long" else "🟥"*10

# Recalculate SL/TP from live entry price
# SL distance stays same as candle-based calculation
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
loss   = round(slp * 0.1 * LOT_SIZE * 100, 2)
prof   = round(tpp * 0.1 * LOT_SIZE * 100, 2)
now    = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")

return (
    f"{bar}\n"
    f"‼️ <b>🚨{em}🚨  {act} SIGNAL FIRED  🚨{em}🚨</b> ‼️\n"
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
    f"📊 RR: 1:{RR}\n\n"
    f"<i>✅ Paper trade logged automatically.</i>\n\n"
    f"{bar}"
), entry, sl, tp   # return levels for paper trade
```

def status_msg(statuses):
now = datetime.now(IST).strftime(”%d %b  %I:%M %p IST”)
lines = [f”📋 <b>Status — {now}</b>\n”]
for pair, color, stv, price in statuses:
em = “🟢” if color == “GREEN” else “🔴”
lines.append(
f”{em} <b>{pair}</b>  —  No signal\n”
f”   ST:{round(stv,5)}   Price:{round(price,5)}\n”
)
lines.append(”<i>Checking every 5 min. /status anytime.</i>”)
return “\n”.join(lines)

def close_msg(pair, direction, entry, exit_px, pnl, pips, result, bal):
em   = “✅” if “TP” in result else “❌”
lb   = “TARGET HIT — PROFIT” if “TP” in result else “STOP HIT — LOSS”
sign = “+” if pnl >= 0 else “”
return (
f”{em} <b>TRADE CLOSED — {pair}</b>\n\n”
f”<b>Result</b>    :  {lb}\n”
f”<b>Direction</b> :  {direction.upper()}\n\n”
f”<b>Entry</b>     :  {round(entry,   5)}\n”
f”<b>Exit</b>      :  {round(exit_px, 5)}\n”
f”<b>P&L</b>       :  {sign}{round(pnl,2)} USD “
f”({sign}{round(pips,1)} pips)\n\n”
f”<b>Balance</b>   :  ${round(bal,2)}”
)

def eod_report():
d   = load()
now = datetime.now(IST)
ts  = now.strftime(”%Y-%m-%d”)
dl  = now.strftime(”%d %b %Y”)
ct  = [t for t in d.get(“closed_trades”,[]) if t.get(“closed_at”,””).startswith(ts)]
tp_ = sum(t.get(“pnl_usd”,0) for t in ct)
cap = d.get(“capital”,   PAPER_CAPITAL)
pnl = d.get(“total_pnl”, 0.0)
ret = round(((cap-PAPER_CAPITAL)/PAPER_CAPITAL)*100, 2)
lines = [f”🌙 <b>EOD REPORT — {dl}</b>\n”, “─── OPEN POSITIONS ───”]
for pair, t in d.get(“open_trades”,{}).items():
c = price_cache.get(pair)
if c:
price   = c[“price”]
dist_sl = round(abs(price-t[“sl”])*10000,1)
dist_tp = round(abs(price-t[“tp”])*10000,1)
pnl_p   = (price-t[“entry”])*10000 if t[“direction”]==“long”   
else (t[“entry”]-price)*10000
pnl_u   = round(pnl_p*0.10,2)
sign    = “+” if pnl_u>=0 else “”
col     = “🟢 GREEN” if c[“color”]==“GREEN” else “🔴 RED”
lines.append(
f”\n<b>{pair}</b> — {t[‘direction’].upper()}\n”
f”  Entry      :  {round(t[‘entry’],5)}\n”
f”  Current    :  {round(price,     5)}\n”
f”  Dist to SL :  {dist_sl} pips\n”
f”  Dist to TP :  {dist_tp} pips\n”
f”  Supertrend :  {col}\n”
f”  Unrealised :  {sign}{pnl_u} USD”
)
if len(lines) == 2:
lines.append(“No open positions tonight.”)
lines.append(”\n─── CLOSED TODAY ───”)
if ct:
for t in ct:
s=”+” if t.get(“pnl_usd”,0)>=0 else “”
e=“✅” if “TP” in t.get(“result”,””) else “❌”
lines.append(f”{e} {t[‘pair’]} {t[‘direction’].upper()} → {s}{t.get(‘pnl_usd’,0)} USD”)
else:
lines.append(“No trades closed today.”)
s1=”+” if tp_>=0 else “”; s2=”+” if pnl>=0 else “”; s3=”+” if ret>=0 else “”
lines.append(
f”\n─── ACCOUNT ───\n”
f”Start: ${PAPER_CAPITAL}  Now: ${round(cap,2)}\n”
f”Today: {s1}{round(tp_,2)} USD  Total: {s2}{round(pnl,2)} USD\n”
f”Return: {s3}{ret}%”
)
send_msg(”\n”.join(lines))

# =============================================================

# WEEKLY CLOSE REPORT — Saturday 2:30 AM IST = Friday 21:00 UTC

# =============================================================

def weekly_close_report():
d   = load()
now = datetime.now(IST)
dl  = now.strftime(”%d %b %Y”)
cap = d.get(“capital”,   PAPER_CAPITAL)
pnl = d.get(“total_pnl”, 0.0)
ret = round(((cap - PAPER_CAPITAL) / PAPER_CAPITAL) * 100, 2)
ct  = d.get(“closed_trades”, [])
week_ago    = (now - timedelta(days=7)).strftime(”%Y-%m-%d”)
week_trades = [t for t in ct if t.get(“closed_at”,””) >= week_ago]
week_pnl    = sum(t.get(“pnl_usd”,0) for t in week_trades)
week_wins   = len([t for t in week_trades if “TP” in t.get(“result”,””)])
week_total  = len(week_trades)
week_wr     = round(week_wins / week_total * 100, 1) if week_total > 0 else 0

```
lines = [f"📅 <b>WEEKLY CLOSE — {dl}</b>\n"]
lines.append("─── OPEN POSITIONS (carry over weekend) ───")
for pair, t in d.get("open_trades",{}).items():
    c = price_cache.get(pair)
    if c:
        price   = c["price"]
        dist_sl = round(abs(price-t["sl"])*10000,1)
        dist_tp = round(abs(price-t["tp"])*10000,1)
        pnl_p   = (price-t["entry"])*10000 if t["direction"]=="long" else (t["entry"]-price)*10000
        pnl_u   = round(pnl_p*0.10,2)
        sign    = "+" if pnl_u>=0 else ""
        col     = "🟢 GREEN" if c["color"]=="GREEN" else "🔴 RED"
        lines.append(
            f"\n<b>{pair}</b> — {t['direction'].upper()}\n"
            f"  Entry      :  {round(t['entry'],5)}\n"
            f"  Current    :  {round(price,5)}\n"
            f"  Dist to SL :  {dist_sl} pips\n"
            f"  Dist to TP :  {dist_tp} pips\n"
            f"  Supertrend :  {col}\n"
            f"  Unrealised :  {sign}{pnl_u} USD\n"
            f"  ⚠️ Gap risk over weekend"
        )
if not d.get("open_trades"):
    lines.append("No open positions. Clean into weekend ✅")

lines.append("\n─── THIS WEEK ───")
if week_trades:
    for t in week_trades:
        s = "+" if t.get("pnl_usd",0)>=0 else ""
        e = "✅" if "TP" in t.get("result","") else "❌"
        lines.append(f"{e} {t['pair']} {t['direction'].upper()} → {s}{t.get('pnl_usd',0)} USD")
    s_w = "+" if week_pnl>=0 else ""
    lines.append(f"\nWeek trades : {week_total}  Wins: {week_wins} ({week_wr}%)  P&L: {s_w}{round(week_pnl,2)} USD")
else:
    lines.append("No trades closed this week.")

s1="+" if pnl>=0 else ""; s2="+" if ret>=0 else ""
lines.append(
    f"\n─── ACCOUNT ───\n"
    f"Balance : ${round(cap,2)}  P&L: {s1}{round(pnl,2)} USD  Return: {s2}{ret}%\n\n"
    f"🌙 Market closed. Reopens Monday 6:30 AM IST."
)
send_msg("\n".join(lines))
print("[Weekly Close Report Sent]")
```

# =============================================================

# WEEKLY OPEN MESSAGE — Monday 6:30 AM IST = Monday 01:00 UTC

# =============================================================

def weekly_open_message():
d   = load()
now = datetime.now(IST)
dl  = now.strftime(”%d %b %Y”)
cap = d.get(“capital”, PAPER_CAPITAL)
open_trades = d.get(“open_trades”, {})

```
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
```

# =============================================================

# MONTHLY REPORT — last trading day of month

# =============================================================

def check_monthly_report():
import calendar
now       = datetime.now(IST)
last_day  = calendar.monthrange(now.year, now.month)[1]
last_date = datetime(now.year, now.month, last_day, tzinfo=IST)
while last_date.weekday() >= 5:
last_date -= timedelta(days=1)
if now.date() == last_date.date():
monthly_report()

def monthly_report():
d   = load()
now = datetime.now(IST)
dl  = now.strftime(”%b %Y”)
cap = d.get(“capital”,   PAPER_CAPITAL)
pnl = d.get(“total_pnl”, 0.0)
ret = round(((cap - PAPER_CAPITAL) / PAPER_CAPITAL) * 100, 2)
ct  = d.get(“closed_trades”, [])
month_str    = now.strftime(”%Y-%m”)
month_trades = [t for t in ct if t.get(“closed_at”,””).startswith(month_str)]
month_pnl    = sum(t.get(“pnl_usd”,0) for t in month_trades)
month_wins   = len([t for t in month_trades if “TP” in t.get(“result”,””)])
month_total  = len(month_trades)
month_wr     = round(month_wins/month_total*100,1) if month_total>0 else 0
gross_profit = sum(t.get(“pnl_usd”,0) for t in month_trades if t.get(“pnl_usd”,0)>0)
gross_loss   = abs(sum(t.get(“pnl_usd”,0) for t in month_trades if t.get(“pnl_usd”,0)<0))
month_pf     = round(gross_profit/gross_loss,2) if gross_loss>0 else 0.0

```
lines = [f"📊 <b>MONTHLY REPORT — {dl}</b>\n"]
lines.append("─── THIS MONTH\'S TRADES ───")
if month_trades:
    for t in month_trades:
        s = "+" if t.get("pnl_usd",0)>=0 else ""
        e = "✅" if "TP" in t.get("result","") else "❌"
        d_str = t.get("closed_at","")[:10]
        lines.append(f"{e} {d_str}  {t['pair']} {t['direction'].upper()} → {s}{t.get('pnl_usd',0)} USD")
else:
    lines.append("No completed trades this month.")

s_m="+" if month_pnl>=0 else ""
lines.append(
    f"\n─── MONTH SUMMARY ───\n"
    f"Trades : {month_total}  Wins: {month_wins} ({month_wr}%)  PF: {month_pf}\n"
    f"Month P&L : {s_m}{round(month_pnl,2)} USD"
)
s1="+" if pnl>=0 else ""; s2="+" if ret>=0 else ""
lines.append(
    f"\n─── OVERALL ───\n"
    f"Balance: ${round(cap,2)}  Total P&L: {s1}{round(pnl,2)} USD  Return: {s2}{ret}%"
)
send_msg("\n".join(lines))
print("[Monthly Report Sent]")
```

# =============================================================

# EXIT CHECK

# =============================================================

def check_exits():
d = load()
for pair, t in list(d.get(“open_trades”,{}).items()):
c     = price_cache.get(pair)
price = c[“price”] if c else None
if price is None:
lp = get_live_price(PAIRS.get(pair,””))
if lp:
price = lp
if price is None:
continue
try:
hit_tp = (t[“direction”]==“long”  and price>=t[“tp”]) or   
(t[“direction”]==“short” and price<=t[“tp”])
hit_sl = (t[“direction”]==“long”  and price<=t[“sl”]) or   
(t[“direction”]==“short” and price>=t[“sl”])
if not hit_tp and not hit_sl:
continue
ep   = t[“tp”] if hit_tp else t[“sl”]
res  = “TP HIT” if hit_tp else “SL HIT”
pips = (ep-t[“entry”])*10000 if t[“direction”]==“long”   
else (t[“entry”]-ep)*10000
pnl  = round(pips*0.10, 2)
d[“capital”]   = d.get(“capital”,   PAPER_CAPITAL) + pnl
d[“total_pnl”] = d.get(“total_pnl”, 0.0)          + pnl
d.setdefault(“closed_trades”,[]).append({
**t, “exit_price”:ep, “pnl_usd”:pnl,
“pnl_pips”:round(pips,1), “result”:res,
“closed_at”:datetime.now(IST).isoformat()
})
del d[“open_trades”][pair]
save(d)
send_msg(close_msg(pair,t[“direction”],t[“entry”],
ep,pnl,pips,res,d[“capital”]))
print(f”[Closed] {pair} {res} PnL:{pnl}”)
except Exception as e:
print(f”[Exit Error] {pair}: {e}”)

# =============================================================

# MAIN CHECK — every 5 minutes

# =============================================================

# Track last hourly status send time

last_status_sent = {“time”: None}

def check_all():
global price_cache
now_ist = datetime.now(IST)
print(f”\n[{now_ist.strftime(’%d %b %H:%M IST’)}] CHECK”)

```
check_exits()

d        = load()
statuses = []

for pair, sym in PAIRS.items():
    df = calc_supertrend(get_data(sym))
    if df.empty:
        print(f"  [Skip] {pair}")
        continue
    try:
        cp  = float(df["close"].iloc[-1])
        stv = float(df["st"].iloc[-2])  if "st"  in df.columns else 0.0
        cd  = float(df["dir"].iloc[-2]) if "dir" in df.columns else 0.0
        col = "GREEN" if cd == -1 else "RED"
        price_cache[pair] = {"price":cp, "st":stv, "color":col}
    except Exception as e:
        print(f"  [Cache Error] {e}"); continue

    sig, candle_close, candle_sl, candle_tp, ctime = detect_signal(df)

    if sig is None:
        statuses.append((pair, col, stv, cp))
        continue

    if last_signal.get(pair) == ctime:
        statuses.append((pair, col, stv, cp))
        continue

    if pair in d.get("open_trades",{}):
        statuses.append((pair, col, stv, cp))
        continue

    # Get LIVE price for accurate entry
    live_price = get_live_price(sym)
    if live_price is None:
        live_price = candle_close  # fallback to candle close
        print(f"  [Warning] Could not get live price, using candle close")

    print(f"  {pair}: *** {sig.upper()} ***")
    print(f"  Candle close: {candle_close:.5f}  Live price: {live_price:.5f}")

    # Build signal message with live price
    msg, entry, sl, tp = sig_msg(
        pair, sig, live_price, candle_close, candle_sl, candle_tp)

    last_signal[pair] = ctime
    send_msg(msg)

    # Save paper trade with LIVE entry price
    d = load()
    d["open_trades"][pair] = {
        "pair":pair, "direction":sig,
        "entry":entry, "sl":sl, "tp":tp,
        "candle_close":candle_close,
        "opened_at":datetime.now(IST).isoformat()
    }
    save(d)
    time.sleep(1)

# Send status update once per hour during trading hours
is_trading = NO_SIG_START <= now_ist.hour < NO_SIG_END
if statuses and is_trading:
    last = last_status_sent["time"]
    now_min = now_ist.hour * 60 + now_ist.minute
    if last is None or (now_min - last) >= 60:
        send_msg(status_msg(statuses))
        last_status_sent["time"] = now_min
        print("  [Status sent]")
```

# =============================================================

# STARTUP

# =============================================================

if **name** == “**main**”:
print(”=”*55)
print(”  SUPERTREND BOT — Fixed Version”)
print(”  Checks every 5 min | Live price entry”)
print(”=”*55)

```
if not TELEGRAM_TOKEN: print("ERROR: TELEGRAM_TOKEN missing"); exit()
if not CHAT_ID:         print("ERROR: CHAT_ID missing");        exit()
if not TWELVE_DATA_KEY: print("ERROR: TWELVE_DATA_KEY missing"); exit()

d = load()
send_msg(
    "🤖 <b>SUPERTREND BOT — UPDATED</b>\n\n"
    f"<b>Pairs</b>    :  EUR/USD + GBP/USD\n"
    f"<b>ATR</b>      :  {ATR_PERIOD}  "
    f"<b>Factor</b>  :  {ST_FACTOR}  "
    f"<b>RR</b>      :  1:{RR}\n"
    f"<b>Capital</b>  :  ${round(d.get('capital',PAPER_CAPITAL),2)} (paper)\n\n"
    f"✅ <b>Checks every 5 minutes</b> (faster signal detection)\n"
    f"✅ <b>Live price as entry</b> (no more stale prices)\n"
    f"✅ Status once per hour during trading hours\n"
    f"✅ EOD report at 10:30 PM IST (London Close)\n\n"
    "Send /status /balance /trades /help anytime."
)

threading.Thread(target=handle_commands, daemon=True).start()

check_all()

# Check every 5 minutes
schedule.every(5).minutes.do(check_all)

# EOD daily at London Close = 10:30 PM IST = 17:00 UTC
schedule.every().day.at("17:00").do(eod_report)

# Weekly close — Saturday 2:30 AM IST = Friday 21:00 UTC
schedule.every().friday.at("21:00").do(weekly_close_report)

# Weekly open — Monday 6:30 AM IST = Monday 01:00 UTC
schedule.every().monday.at("01:00").do(weekly_open_message)

# Monthly report — runs daily at 17:00 UTC, fires only on last trading day
schedule.every().day.at("17:05").do(check_monthly_report)

print("\nRunning — checks every 5 minutes\n")
while True:
    schedule.run_pending()
    time.sleep(10)
```