# Setup checklist

Follow these steps in order. Everything here uses the OANDA **practice (demo) account**. Real money
is only switched on after Gate D (see the design spec, section 10). Never set
`LIVE_TRADING_CONFIRMED=yes` before then.

## 1. OANDA practice account

1. Open a free practice account at OANDA (UK). Choose **GBP** as the account currency.
2. In the OANDA hub, generate a personal **API access token** (Manage API Access).
3. Note your **v20 account ID** (looks like `101-004-1234567-001`).
4. Do not trade manually on this account while the bot runs. The bot pauses if it finds trades or
   orders it did not create.

## 2. Check the connection (from your computer)

Requires Python 3.12. In this folder:

```bash
OANDA_PRACTICE_TOKEN=your-token OANDA_PRACTICE_ACCOUNT=your-account-id python -m unittest tests.test_fx_oanda_integration -v
```

Run it while the market is open (Sunday evening to Friday evening: 22:00 UK, or 21:00 UK during
the few weeks a year when US and UK clock changes differ). It opens and closes one
1-unit EUR/USD trade. All tests must pass: they confirm that OANDA's real responses match what the
bot expects. If anything fails, stop and report it; do not deploy.

## 3. Backtest (Gate B)

1. Download several years of history (about 2 GB of CSV for 8 years):

   ```bash
   OANDA_API_TOKEN=your-token OANDA_ACCOUNT_ID=your-account-id python -m fxbot fetch-data --start 2016-01-01 --end 2025-01-01 --out data
   ```

   It prints a gap report (missing 15-minute candles per pair and month). A few gaps are normal.
2. Get a historical calendar of **scheduled high-impact** releases as `news.csv` with header
   `time,currency,impact` (UTC times, e.g. `2024-01-05T13:30:00+00:00,USD,High`).
3. Run the research report (a full run with 1,000 no-skill runs takes many CPU-hours; try
   `--no-skill-runs 50` first):

   ```bash
   python -m fxbot backtest --data data --news news.csv --s10 data/s10.csv --start 2016-03-01 --end 2025-01-01
   ```

4. Read `runtime/research/research.md`. Gate B passes only if every automated check passes **and**
   the per-year and per-pair tables look robust to you.

## 4. Telegram (alerts and phone controls)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the **bot token**.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `message.chat.id`.
3. Commands once running: `/status`, `/pause`, `/resume` (reply YES), `/flatten` (reply YES).
   `/flatten` closes every bot trade **and pauses** new trades; send `/resume` to trade again.
   Commands sent while the bot was offline are ignored (older than 2 minutes).

## 5. Dead-man's switch

1. Create a free check at healthchecks.io with **period 5 minutes** and **grace 10 minutes**.
2. Connect it to Telegram or email (Integrations) so you are told if the bot stops.
3. Copy the ping URL.

## 6. Railway

1. Create a project from this GitHub repository. Railway uses `Dockerfile` and `railway.json`.
2. Add a **volume** mounted at `/data` (the ledger lives there).
3. Confirm in the service settings: **1 replica**, restart policy **on failure**.
4. Variables:

   | Variable | Value |
   |---|---|
   | `OANDA_API_TOKEN` | practice token |
   | `OANDA_ACCOUNT_ID` | practice account ID |
   | `OANDA_ENV` | `practice` |
   | `TELEGRAM_BOT_TOKEN` | from BotFather |
   | `TELEGRAM_CHAT_ID` | your chat ID |
   | `HEALTHCHECK_URL` | healthchecks.io ping URL |
   | `TRADING_ENABLED` | `true` or `false` (`false` pauses at startup; setting it back to `true` does not undo a pause, send `/resume`) |

5. Deploy. You should receive "fxbot started on the OANDA practice account" in Telegram.

**Alerts you should expect:** fills, closes and a daily summary; a warning if no new trades have
been possible for over an hour (with the reason); a warning if Telegram commands cannot be read.

**First day:** a brand-new ledger on an account that already has history blocks new trades for
the rest of that UK day ("no reliable daily snapshot today"). This is expected; trading starts the
next day.

## 7. Demo period (Gate C)

Run for at least 8 weeks (target 12). During that time, deliberately test: `/pause` and `/resume`,
`/flatten`, a Railway restart, and watch at least two Friday flattens. Compare trade frequency and
real slippage with the backtest. There must be zero unresolved-order or missing-stop incidents.

## 8. Real money (Gate D)

Only after Gates A to C pass: work through the Gate D checklist in the spec, create a separate live
API token, and then you, not the bot, set `OANDA_ENV=live` and `LIVE_TRADING_CONFIRMED=yes`.
