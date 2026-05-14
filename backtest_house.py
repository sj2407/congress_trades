"""Backtest using the historical House dataset (real disclosure dates).

Reads data/cache/house_historical.json (produced by backfill_house.py) and
emits data/backtest_house.csv with the same schema as backtest.py — except
`lag_days` is REAL, not assumed 30. This enables the lag-bucket analysis.

Run:  python backtest_house.py [--limit N] [--out data/backtest_house.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from src.prices import _history, latest_price, price_on_or_after

HOUSE_PATH = "data/cache/house_historical.json"


def _parse_iso(s: str | None) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _direction(tx_type: str) -> int:
    t = (tx_type or "").lower()
    if "urchase" in t:
        return 1
    if "ale" in t:
        return -1
    return 0


def run(limit: Optional[int], out_path: str) -> None:
    with open(HOUSE_PATH) as f:
        records = json.load(f)
    today = date.today()
    # Keep purchases + sales with valid dates + ticker, and 90d post-disclosure
    # already in the past so the +30/+90 windows are observable
    pool = []
    for r in records:
        tx = _parse_iso(r.get("transaction_date"))
        dd = _parse_iso(r.get("disclosure_date"))
        d = _direction(r.get("transaction_type", ""))
        if not tx or not dd or not r.get("ticker") or d == 0:
            continue
        if dd + timedelta(days=90) > today:
            continue
        pool.append({**r, "_tx": tx, "_dd": dd, "_d": d})
    if limit:
        pool = pool[:limit]

    unique = sorted({r["ticker"] for r in pool})
    print(f"Backtesting {len(pool):,} House trades across {len(unique):,} tickers…", flush=True)
    for i, tk in enumerate(unique, 1):
        _history(tk)
        if i % 200 == 0:
            print(f"  prefetched {i}/{len(unique)}", flush=True)

    fieldnames = [
        "member", "ticker", "tx_type", "direction",
        "trade_date", "disclosure_date", "lag_days",
        "price_trade", "price_disclose", "price_disclose_30", "price_disclose_90", "price_today",
        "ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct", "ret_to_today_pct",
        "captured_share_pct", "amount_range", "ptr_url",
    ]

    sum_g, sum_p, n_v = 0.0, 0.0, 0
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(pool, 1):
            tx, dd, d, tk = r["_tx"], r["_dd"], r["_d"], r["ticker"]
            p_trade = price_on_or_after(tk, tx)
            p_disc = price_on_or_after(tk, dd)
            p_disc30 = price_on_or_after(tk, dd + timedelta(days=30))
            p_disc90 = price_on_or_after(tk, dd + timedelta(days=90))
            p_today = latest_price(tk)

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
                sum_g += ret_gap
                sum_p += ret_30
                n_v += 1

            w.writerow({
                "member": r["member"],
                "ticker": tk,
                "tx_type": r["transaction_type"],
                "direction": d,
                "trade_date": tx.isoformat(),
                "disclosure_date": dd.isoformat(),
                "lag_days": r["lag_days"],
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
                "amount_range": r.get("amount_range", ""),
                "ptr_url": r.get("ptr_url", ""),
            })
            if i % 500 == 0:
                print(f"  ...{i}/{len(pool)}", flush=True)
    print(f"Wrote {out_path}")
    if n_v:
        print(f"Summary on {n_v} comparable trades (direction-adjusted):")
        print(f"  mean return DURING the gap (trade->disclosure): {sum_g/n_v:.2f}%")
        print(f"  mean return AFTER disclosure (disclosure->+30d): {sum_p/n_v:.2f}%")
        if sum_g + sum_p:
            print(f"  -> {sum_g/(sum_g+sum_p)*100:.1f}% of the 30-day move was already captured by disclosure")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default="data/backtest_house.csv")
    args = p.parse_args()
    if not os.path.exists(HOUSE_PATH):
        print(f"ERROR: {HOUSE_PATH} not found. Run backfill_house.py first.", file=sys.stderr)
        return 1
    run(args.limit, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
