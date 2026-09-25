"""
Monthly RSI(2) band-cross + reversal alert agent for Nifty indices.

Mirrors the TradingView RSI settings in use:
  RSI length 2, source close, RSI-based MA = SMA 14, bands 80 / 50 / 20, timeframe 1M.
RSI is Wilder's RMA exactly as TradingView's ta.rsi computes it.

Signal state machine per index (evaluated on the live, still-forming monthly bar, same as TV):
  NEUTRAL  --RSI < 20-->  OVERSOLD   : email "entered oversold, watching"
  OVERSOLD --tracks lowest RSI while it keeps falling (no email)--
  OVERSOLD --RSI crosses back > 20-->  NEUTRAL : email "BUY"
  NEUTRAL  --RSI > 80-->  OVERBOUGHT : email "entered overbought"
  OVERBOUGHT --RSI crosses back < 80--> NEUTRAL : email "trim / avoid fresh buys"
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- config
RSI_LEN = 2
MA_LEN = 14
UPPER, MIDDLE, LOWER = 80.0, 50.0, 20.0
ALERT_OVERBOUGHT = True  # set False for buy-side alerts only

# First ticker that returns data is used (Yahoo symbols shift occasionally).
INDICES = {
    "Nifty 100":           ["^CNX100", "NIFTY_100.NS"],
    "Nifty Next 50":       ["^NSMIDCP", "NIFTY_NEXT_50.NS"],
    "Nifty Midcap 100":    ["NIFTY_MIDCAP_100.NS", "^CNXMIDCAP", "^CRSMID"],
    "Nifty Smallcap 100":  ["^CNXSC", "NIFTY_SMLCAP_100.NS"],
}

STATE_FILE = Path(__file__).with_name("state.json")
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- indicators
def tv_rma(src: pd.Series, length: int) -> pd.Series:
    """TradingView ta.rma: seeded with SMA of first `length` values, then Wilder smoothing."""
    vals = src.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    alpha = 1.0 / length
    start = None
    for i in range(len(vals)):
        if np.isnan(vals[i]):
            continue
        if start is None:
            window = vals[max(0, i - length + 1): i + 1]
            if len(window) == length and not np.isnan(window).any():
                out[i] = window.mean()
                start = i
            continue
        out[i] = alpha * vals[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=src.index)


def tv_rsi(close: pd.Series, length: int = RSI_LEN) -> pd.Series:
    change = close.diff()
    up = tv_rma(change.clip(lower=0), length)
    down = tv_rma((-change).clip(lower=0), length)
    rsi = np.where(down == 0, 100.0, np.where(up == 0, 0.0, 100 - 100 / (1 + up / down)))
    return pd.Series(rsi, index=close.index).where(up.notna() & down.notna())


def monthly_close(daily: pd.Series) -> pd.Series:
    """Month bars from daily closes; last bar is the live, forming month (as on TV)."""
    return daily.dropna().resample("ME").last().dropna()


# ---------------------------------------------------------------- data
def fetch_daily(tickers: list[str]) -> tuple[str, pd.Series]:
    import yfinance as yf

    for t in tickers:
        try:
            df = yf.download(t, period="20y", interval="1d", progress=False, auto_adjust=False)
        except Exception as e:  # noqa: BLE001
            print(f"  {t}: {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) > 300:
            return t, close
    raise RuntimeError(f"No data for any of {tickers}")


# ---------------------------------------------------------------- signals
def evaluate(name: str, rsi_now: float, prev: dict) -> tuple[dict, list[dict]]:
    """Advance one index's state. A band-to-band jump yields exit + entry in one run."""
    state, first = _step(rsi_now, prev)
    events = [first] if first else []
    if first and first["type"] in ("BUY", "OVERBOUGHT_EXIT"):
        state, second = _step(rsi_now, state)
        if second:
            events.append(second)
    return state, events


def _step(rsi_now: float, prev: dict) -> tuple[dict, dict | None]:
    zone = prev.get("zone", "NEUTRAL")
    extreme = prev.get("extreme")
    event = None

    if zone == "NEUTRAL":
        if rsi_now < LOWER:
            zone, extreme = "OVERSOLD", rsi_now
            event = {"type": "OVERSOLD_ENTRY", "rsi": rsi_now}
        elif ALERT_OVERBOUGHT and rsi_now > UPPER:
            zone, extreme = "OVERBOUGHT", rsi_now
            event = {"type": "OVERBOUGHT_ENTRY", "rsi": rsi_now}

    elif zone == "OVERSOLD":
        if rsi_now < LOWER:
            extreme = min(extreme if extreme is not None else rsi_now, rsi_now)
        else:  # crossed back above 20 -> reversal confirmed
            event = {"type": "BUY", "rsi": rsi_now, "low": extreme,
                     "since": prev.get("since")}
            zone, extreme = "NEUTRAL", None

    elif zone == "OVERBOUGHT":
        if rsi_now > UPPER:
            extreme = max(extreme if extreme is not None else rsi_now, rsi_now)
        else:
            event = {"type": "OVERBOUGHT_EXIT", "rsi": rsi_now, "high": extreme,
                     "since": prev.get("since")}
            zone, extreme = "NEUTRAL", None

    since = prev.get("since")
    if event and event["type"].endswith("_ENTRY"):
        since = datetime.now(IST).strftime("%Y-%m-%d")
    if zone == "NEUTRAL":
        since = None
    return {"zone": zone, "extreme": extreme, "since": since}, event


def seed_zone(rsi: pd.Series) -> dict:
    """First run: infer current zone from history so we don't fire stale alerts."""
    last = float(rsi.iloc[-1])
    if last < LOWER:
        return {"zone": "OVERSOLD", "extreme": last, "since": None}
    if ALERT_OVERBOUGHT and last > UPPER:
        return {"zone": "OVERBOUGHT", "extreme": last, "since": None}
    return {"zone": "NEUTRAL", "extreme": None, "since": None}


# ---------------------------------------------------------------- email
def build_email(rows: list[dict], events: list[tuple[str, dict]]) -> tuple[str, str]:
    buys = [n for n, e in events if e["type"] == "BUY"]
    if buys:
        subject = f"BUY: {', '.join(buys)} — monthly RSI(2) reversed up through 20"
    else:
        labels = {"OVERSOLD_ENTRY": "below 20", "OVERBOUGHT_ENTRY": "above 80",
                  "OVERBOUGHT_EXIT": "back below 80"}
        subject = "RSI alert: " + "; ".join(f"{n} {labels[e['type']]}" for n, e in events)

    lines = []
    for n, e in events:
        t = e["type"]
        if t == "BUY":
            lines.append(f"BUY  {n}: RSI {e['rsi']:.2f} crossed back above {LOWER:.0f} "
                         f"(low in this dip {e['low']:.2f}"
                         + (f", oversold since {e['since']})" if e.get("since") else ")"))
        elif t == "OVERSOLD_ENTRY":
            lines.append(f"WATCH {n}: RSI {e['rsi']:.2f} dropped below {LOWER:.0f}. "
                         f"Buy alert will follow when it turns back above {LOWER:.0f}.")
        elif t == "OVERBOUGHT_ENTRY":
            lines.append(f"HOT  {n}: RSI {e['rsi']:.2f} above {UPPER:.0f}. Avoid fresh lump-sum buys.")
        elif t == "OVERBOUGHT_EXIT":
            lines.append(f"TRIM {n}: RSI {e['rsi']:.2f} fell back below {UPPER:.0f} "
                         f"(peak {e['high']:.2f}).")

    table = ["", f"{'Index':<20}{'Close':>12}{'RSI(2)':>9}{'MA14':>8}  Zone",
             "-" * 60]
    for r in rows:
        table.append(f"{r['name']:<20}{r['close']:>12,.2f}{r['rsi']:>9.2f}"
                     f"{r['ma']:>8.2f}  {r['zone']}")
    stamp = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    body = "\n".join(lines + table + [
        "",
        f"Monthly timeframe, live (forming) bar as of {stamp}. "
        f"RSI {RSI_LEN} close, bands {UPPER:.0f}/{MIDDLE:.0f}/{LOWER:.0f}.",
        "Not investment advice.",
    ])
    return subject, body


def send_email(subject: str, body: str) -> None:
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


# ---------------------------------------------------------------- main
def run(data: dict[str, pd.Series] | None = None, dry_run: bool = False) -> list:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    idx_state = state.get("indices", {})
    rows, events = [], []

    for name, tickers in INDICES.items():
        if data is not None:
            ticker, daily = "test", data[name]
        else:
            try:
                ticker, daily = fetch_daily(tickers)
            except RuntimeError as e:
                print(f"SKIP {name}: {e}", file=sys.stderr)
                continue
        m = monthly_close(daily)
        rsi = tv_rsi(m, RSI_LEN)
        ma = rsi.rolling(MA_LEN).mean()
        rsi_now = float(rsi.iloc[-1])

        prev = idx_state.get(name)
        if prev is None:
            new, evs = seed_zone(rsi), []
        else:
            new, evs = evaluate(name, rsi_now, prev)
        idx_state[name] = new | {"ticker": ticker, "rsi": round(rsi_now, 2)}
        events += [(name, e) for e in evs]
        rows.append({"name": name, "close": float(m.iloc[-1]), "rsi": rsi_now,
                     "ma": float(ma.iloc[-1]), "zone": new["zone"]})
        print(f"{name:<20} {ticker:<20} RSI {rsi_now:6.2f}  {new['zone']}"
              + "".join(f"  -> {e['type']}" for e in evs))

    if events:
        subject, body = build_email(rows, events)
        print("\n" + subject + "\n" + body)
        if not dry_run:
            send_email(subject, body)

    state["indices"] = idx_state
    state["last_run"] = datetime.now(IST).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return events


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
