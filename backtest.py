"""One-off backtest of the disclosure-gap effect.

The senate-stock-watcher dataset doesn't carry per-record disclosure dates,
so we approximate the disclosure date as `transaction_date + ASSUMED_LAG_DAYS`
(default 30 — within the legal 45-day window and consistent with academic
studies of typical congressional reporting lag).

For every historical Senate purchase or sale we compute:
  - price on transaction_date          (insider entry)
  - price on assumed disclosure_date   (what a follower would have seen)
  - price 30 / 90 days after disclosure

Direction-adjusted (purchases positive when stock rises, sales positive when
stock falls), we then ask:
  - How much of the eventual move had already happened by disclosure?
  - How much was still capturable for a follower entering at disclosure?

Run with:
  python backtest.py [--limit N] [--lag-days 30] [--out data/backtest.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from src.fetch_senate import fetch_senate_trades
from src.types import Trade

PURCHASE_KEYWORDS = ("purchase",)
SALE_KEYWORDS = ("sale",)


def _direction(tx_type: str) -> int:
    t = (tx_type or "").lower()
    if any(k in t for k in PURCHASE_KEYWORDS):
        return 1
    if any(k in t for k in SALE_KEYWORDS):
        return -1
    return 0


_HISTORY_CACHE: dict[str, Optional[pd.DataFrame]] = {}


def _load_history(ticker: str) -> Optional[pd.DataFrame]:
    """Fetch the full price history for a ticker once and cache it in-process."""
    if ticker in _HISTORY_CACHE:
        return _HISTORY_CACHE[ticker]
    df: Optional[pd.DataFrame] = None
    try:
        df = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        if df is None or df.empty:
            df = None
        else:
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    except Exception:
        df = None
    _HISTORY_CACHE[ticker] = df
    return df


def _price_on_or_after(ticker: str, target: date, window_days: int = 7) -> Optional[float]:
    """Closing price on `target` or the first trading day within `window_days` after."""
    if not ticker or not target:
        return None
    df = _load_history(ticker)
    if df is None or df.empty:
        return None
    target_ts = pd.Timestamp(target)
    window_end = target_ts + pd.Timedelta(days=window_days)
    slice_ = df.loc[(df.index >= target_ts) & (df.index <= window_end)]
    if slice_.empty:
        return None
    return float(slice_["Close"].iloc[0])


def run(limit: Optional[int], out_path: str, lag_days: int) -> None:
    trades = fetch_senate_trades()
    today = date.today()
    # only keep records with both dates and a ticker, and old enough that
    # disclosure + 90d window is in the past
    cutoff = today - timedelta(days=lag_days + 90)
    pool = [
        t for t in trades
        if t.transaction_date and t.ticker and t.amount_range
        and _direction(t.transaction_type) != 0
        and t.transaction_date <= cutoff
    ]
    if limit:
        pool = pool[:limit]

    # Pre-warm price history per unique ticker so each trade is a fast lookup.
    unique = sorted({t.ticker for t in pool if t.ticker})
    print(f"Backtesting {len(pool)} trades across {len(unique)} unique tickers (assumed lag = {lag_days}d)…", flush=True)
    for i, tk in enumerate(unique, 1):
        _load_history(tk)
        if i % 100 == 0:
            print(f"  prefetched {i}/{len(unique)} tickers", flush=True)
    fieldnames = [
        "member", "ticker", "tx_type", "direction",
        "trade_date", "disclosure_date", "lag_days",
        "price_trade", "price_disclose", "price_disclose_30", "price_disclose_90", "price_today",
        "ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct", "ret_to_today_pct",
        "captured_share_pct", "amount_range", "ptr_url",
    ]

    sum_during, sum_post30, n_valid = 0.0, 0.0, 0

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, t in enumerate(pool, 1):
            d = _direction(t.transaction_type)
            assumed_disclosure = t.transaction_date + timedelta(days=lag_days)
            p_trade = _price_on_or_after(t.ticker, t.transaction_date)
            p_disc = _price_on_or_after(t.ticker, assumed_disclosure)
            p_disc30 = _price_on_or_after(t.ticker, assumed_disclosure + timedelta(days=30))
            p_disc90 = _price_on_or_after(t.ticker, assumed_disclosure + timedelta(days=90))
            p_today = _price_on_or_after(t.ticker, today - timedelta(days=5))

            def pct(a, b):
                if a is None or b is None or a == 0:
                    return None
                return round(d * (b - a) / a * 100, 3)

            ret_gap = pct(p_trade, p_disc)
            ret_30 = pct(p_disc, p_disc30)
            ret_90 = pct(p_disc, p_disc90)
            ret_today = pct(p_trade, p_today)

            captured = None
            if ret_gap is not None and ret_today is not None and abs(ret_today) > 1e-6:
                captured = round(ret_gap / ret_today * 100, 1)

            if ret_gap is not None and ret_30 is not None:
                sum_during += ret_gap
                sum_post30 += ret_30
                n_valid += 1

            w.writerow({
                "member": t.member_name,
                "ticker": t.ticker,
                "tx_type": t.transaction_type,
                "direction": d,
                "trade_date": t.transaction_date.isoformat() if t.transaction_date else "",
                "disclosure_date": t.disclosure_date.isoformat(),
                "lag_days": t.disclosure_lag_days,
                "price_trade": p_trade,
                "price_disclose": p_disc,
                "price_disclose_30": p_disc30,
                "price_disclose_90": p_disc90,
                "price_today": p_today,
                "ret_during_gap_pct": ret_gap,
                "ret_post_30_pct": ret_30,
                "ret_post_90_pct": ret_90,
                "ret_to_today_pct": ret_today,
                "captured_share_pct": captured,
                "amount_range": t.amount_range,
                "ptr_url": t.ptr_url or "",
            })

            if i % 50 == 0:
                print(f"  ...{i}/{len(pool)}", flush=True)

    print(f"Wrote {out_path}")
    if n_valid:
        print(
            f"Summary on {n_valid} comparable trades (direction-adjusted):\n"
            f"  mean return DURING the gap (trade->disclosure): {sum_during/n_valid:.2f}%\n"
            f"  mean return AFTER disclosure (disclosure->+30d): {sum_post30/n_valid:.2f}%\n"
            f"  -> {sum_during/(sum_during+sum_post30)*100:.1f}% of the 30-day move was already captured by disclosure"
            if (sum_during + sum_post30) != 0 else ""
        )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="Cap number of trades to backtest")
    p.add_argument("--lag-days", type=int, default=30,
                   help="Assumed days between transaction and disclosure (default 30)")
    p.add_argument("--out", default="data/backtest.csv")
    args = p.parse_args()
    run(args.limit, args.out, args.lag_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
