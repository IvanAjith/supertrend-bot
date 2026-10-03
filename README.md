# Supertrend paper-trading bot

Paper-trades EUR/USD and GBP/USD on 1H candles (Supertrend ATR 13, factor 4.11,
RR 1:1.7, $300 account) and reports to Telegram. Runs free on GitHub Actions.

## One-time setup
1. Repo **Settings → Secrets and variables → Actions → New repository secret**, add:
   `TELEGRAM_TOKEN`, `CHAT_ID`, `TWELVE_DATA_KEY`
2. Repo **Settings → Actions → General → Workflow permissions** → *Read and write permissions*
3. **Actions** tab → *Supertrend Bot* → **Run workflow** to test. It then runs about every 5 minutes.

## Notes
- State (balance, open/closed trades) lives in `paper_trades.json` and is committed by the bot.
- Telegram commands (`/status`, `/balance`, `/trades`, `/help`) are answered on the next run.
- Strategy settings are at the top of `supertrend_bot.py`.
- GitHub disables scheduled runs after 60 days with no repo activity; the bot's own commits count as activity.

## Telegram commands
`/status` `/balance` `/trades` `/log` (last 5 closed trades) `/month` (month summary) `/help`

## Trade history
Every trade is stored in `paper_trades.json` (`closed_trades`): id, pair, direction, entry, stop, target,
pips, risk/reward in $, signal candle time, open/close time, duration, result, P&L and balance after.
Power BI / Excel can read it straight from the raw GitHub URL of that file.

## TradingView replica
`tradingview/supertrend_paper_bot.pine` — paste into the Pine Editor on a **1H** chart. It draws the same
BUY/SELL trade (entry, stop, target, risk/reward) and the TP/SL result, plus a paper-account table, so you can
compare each Telegram message with the chart. The Telegram signal includes a "Match on TradingView" block
(flip candle time + candle close) for that comparison.
