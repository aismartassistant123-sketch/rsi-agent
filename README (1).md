# Monthly RSI(2) alert agent — Nifty 100 / Next 50 / Midcap 100 / Smallcap 100

Runs on GitHub Actions every weekday at 16:15 IST. Emails you only when something happens.

## Emails you get
| Event | Trigger | Subject |
|---|---|---|
| WATCH | Monthly RSI(2) drops below 20 | `RSI alert: <index> below 20` |
| **BUY** | RSI crosses back above 20, however low it went | `BUY: <index> — monthly RSI(2) reversed up through 20` |
| HOT | RSI goes above 80 | `RSI alert: <index> above 80` |
| TRIM | RSI falls back below 80 | `RSI alert: <index> back below 80` |

Each email has a table with all four indices: close, RSI, RSI-MA(14), zone.
To get buy-side alerts only, set `ALERT_OVERBOUGHT = False` in `rsi_agent.py`.

## Setup (10 min)
1. Create a **private** GitHub repo and upload these files, keeping `.github/workflows/rsi.yml` at that path.
2. Gmail: turn on 2-Step Verification → https://myaccount.google.com/apppasswords → create an app password (16 characters).
3. In the repo: Settings → Secrets and variables → Actions → New repository secret:
   - `GMAIL_USER` — your Gmail address
   - `GMAIL_APP_PASSWORD` — the 16-character app password
   - `ALERT_TO` — optional; where to send, comma-separated (defaults to `GMAIL_USER`)
4. Settings → Actions → General → Workflow permissions → **Read and write**. The agent commits `state.json` to remember where each index is between runs.
5. Actions tab → "Monthly RSI alerts" → **Run workflow** once. The first run records the current zones and sends nothing. Later runs email you only on a change.

## Local test
```
pip install -r requirements.txt
python rsi_agent.py --dry-run     # prints, doesn't email
```

## Notes
- The RSI matches TradingView's `ta.rsi` (Wilder RMA). Monthly bars come from daily closes, so the current month is the live, still-forming bar, as on your TV chart. An intra-month cross can reverse before the month closes.
- Yahoo tickers per index are in `INDICES`; the first one that returns data is used. If Yahoo renames a symbol, add the new one to the list.
- GitHub pauses scheduled workflows after 60 days with no repo activity. The daily `state.json` commit keeps it active.
