# Supertrend paper-trading bot

Paper-trades GBP/USD on 1H candles (Supertrend ATR 13, factor 4.5, RR 1:1.5, $300 account,
0.01 lot) and reports to Telegram. Runs free on GitHub Actions.

## Settings — keep these three files in sync
| Setting | Value | Where |
|---|---|---|
| Pair | GBP/USD | `PAIRS` in `supertrend_bot.py` |
| Timeframe | 1H | (fixed) |
| ATR period | 13 | `ATR_PERIOD` / Pine `atrLen` |
| Supertrend factor | 4.5 | `ST_FACTOR` / Pine `factor` |
| Reward : Risk | 1.5 | `RR` / Pine `rr` |
| Lot | 0.01 ($0.10 per pip) | `LOT_SIZE` / Pine `lotSize` |

| Spread per trade | 1.5 pips (taken off every result) | `SPREAD_PIPS` / Pine `spreadPip` |
| Filters | ADX and EMA filters off | `strategy_settings.json` / Pine filter inputs |

The bot reads ATR, factor, RR and filters from `strategy_settings.json` (the learning review updates it after
`/approve`). Files: `supertrend_bot.py` (the bot), `learner.py` (journal + learning),
`tradingview/supertrend_paper_bot.pine` (indicator replica), `tradingview/supertrend_strategy.pine`
(strategy for the TradingView Strategy Tester). If you change a number, change it everywhere.

## Backtest summary (GBP/USD, 1H, factor 4.5, RR 1.5, 0.01 lot, about 1.5 pip cost)
On 2012–Mar 2022 hourly data (OANDA and broker data, cross-checked): profit factor about 1.25,
about 3 trades a month, 45% winners, roughly 60% of months profitable. Average stop about 110 pips,
so each trade risks about $11 at 0.01 lot. Results vary a lot by year (2017: −$117, 2018: +$240),
and the worst peak-to-trough drop was about $160. Paper-trade before using real money.

## One-time setup
1. Repo **Settings → Secrets and variables → Actions → New repository secret**, add:
   `TELEGRAM_TOKEN`, `CHAT_ID`, `TWELVE_DATA_KEY`
2. Repo **Settings → Actions → General → Workflow permissions** → *Read and write permissions*
3. **Actions** tab → *Supertrend Bot* → **Run workflow** to test. It then runs about every 5 minutes.

## Notes
- State (balance, open/closed trades) lives in `paper_trades.json` and is committed by the bot.
- Telegram commands (`/status`, `/balance`, `/trades`, `/help`) are answered on the next run.
- The bot fetches 500 hourly candles per run so its Supertrend line matches TradingView exactly
  (with only 100 candles it disagreed about 1% of the time).
- GitHub disables scheduled runs after 60 days with no repo activity; the bot's own commits count as activity.

## Telegram commands
`/status` `/balance` `/trades` `/log` (last 5 closed trades) `/month` (month summary) `/help`
`/learn` (lessons + re-test now) `/settings` (active settings, pending proposal) `/approve` `/reject`

## Learning (learner.py)
- **Journal:** every trade stores why it was taken (session, ADX, with/against EMA200, stop size,
  length of the previous trend) and how it went (best and worst point before the exit, hours held).
  Each close message says, for example, "best point was +1.1R before the stop — it was well in profit, then reversed".
- **Monthly review** (last trading day, after the monthly report, or any time with `/learn`): lessons from the
  journal grouped by session, trend strength, trend direction and stop size, plus a re-test of the strategy
  on ~2.5 years of hourly candles (3 × 5,000 candles from Twelve Data, 3 API credits).
- **Guarded changes:** only settings inside the tested safe range can be proposed (factor 4.0–5.0, RR 1.3–1.7,
  ADX and EMA filters on/off). A proposal needs at least 40 back-test trades, must beat the current settings by
  0.10 profit factor in **both** halves of the history, and its neighbouring settings must also be profitable.
  `/approve` applies it (saved in `strategy_settings.json` with the evidence); set `AUTO_APPLY = True` in
  `supertrend_bot.py` to skip the approval step.
- **Why not change after every trade?** At ~3 trades a month, a few months of paper trades is too little data —
  tuning on it just chases luck. Lessons come from the journal; changes need the long re-test to agree.
- After any change, put the same numbers into both TradingView scripts.

## Trade history
Every trade is stored in `paper_trades.json` (`closed_trades`): id, pair, direction, entry, stop, target,
pips, risk/reward in $, signal candle time, open/close time, duration, result, P&L and balance after.
Power BI / Excel can read it straight from the raw GitHub URL of that file.

## TradingView
- `tradingview/supertrend_paper_bot.pine` — indicator. Paste into the Pine Editor on a **1H** chart. It draws
  the same BUY/SELL trade (entry, stop, target, risk/reward) and the TP/SL result, plus a paper-account table,
  so you can compare each Telegram message with the chart. The Telegram signal includes a
  "Match on TradingView" block (flip candle time + candle close) for that comparison.
- `tradingview/supertrend_strategy.pine` — strategy. Create a new **strategy** in the Pine Editor, paste it,
  and the Strategy Tester shows profit, profit factor, drawdown and a monthly profit table.
