"""One-off historical backfill of House PTR filings.

For each requested year:
  1. Download the official Clerk FD.zip index
  2. Concurrently fetch + parse each PTR PDF
  3. Cache the parsed trades to data/cache/house_{year}.json
  4. Skip years whose cache file already exists (idempotent — safe to re-run)

Output: a per-year JSON of normalized Trade dicts (real disclosure_date from
the XML index, real transaction_date + ticker + amount from the PDF), plus
a combined data/cache/house_historical.json across all years.

Run:  python backfill_house.py --years 2022,2023,2024,2025 [--workers 12]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import List

import requests

from src.fetch_house import (
    CLERK_PDF_TMPL,
    CLERK_ZIP_TMPL,
    _parse_date,
    _parse_ptr_pdf,
    fetch_house_index,
)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")


def _fetch_and_parse_one(entry: dict) -> dict:
    """Download one PTR PDF and return a serialized record (list of trade dicts).

    Returns: {doc_id, year, member, state, filing_date, pdf_url, trades: [...]}
    """
    year = entry["year"]
    doc_id = entry["doc_id"]
    pdf_url = CLERK_PDF_TMPL.format(year=year, doc=doc_id)
    out = {
        "doc_id": doc_id,
        "year": year,
        "member": f"{entry['first']} {entry['last']}".strip(),
        "state": entry["state_dst"][:2] if entry["state_dst"] else None,
        "filing_date": entry["filing_date"].isoformat() if entry["filing_date"] else None,
        "pdf_url": pdf_url,
        "trades": [],
        "error": None,
    }
    try:
        r = requests.get(pdf_url, timeout=60)
        if r.status_code != 200:
            out["error"] = f"http {r.status_code}"
            return out
        parsed = _parse_ptr_pdf(r.content)
        for ticker, asset, tx_type, tx_date, _notif, amt, owner in parsed:
            if not ticker or not tx_date:
                continue
            out["trades"].append({
                "ticker": ticker,
                "asset_description": asset,
                "transaction_type": tx_type,
                "transaction_date": tx_date.isoformat() if tx_date else None,
                "amount_range": amt,
                "owner": owner,
            })
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def backfill_year(year: int, workers: int) -> List[dict]:
    cache_path = os.path.join(CACHE_DIR, f"house_{year}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            records = json.load(f)
        print(f"[{year}] cached: {len(records)} PTRs", flush=True)
        return records

    print(f"[{year}] fetching index…", flush=True)
    index = fetch_house_index(year)
    print(f"[{year}] {len(index)} PTRs to process with {workers} workers", flush=True)

    records: List[dict] = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_and_parse_one, e): e for e in index}
        done = 0
        for fut in as_completed(futures):
            records.append(fut.result())
            done += 1
            if done % 100 == 0 or done == len(index):
                elapsed = time.time() - start
                rate = done / elapsed if elapsed else 0
                remaining = (len(index) - done) / rate if rate else 0
                print(f"[{year}]   {done}/{len(index)}  ({rate:.1f}/s, ~{remaining/60:.1f} min left)", flush=True)

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"[{year}] wrote {cache_path}: {len(records)} PTRs", flush=True)
    return records


def _combine(years: List[int]) -> str:
    """Merge per-year caches into a single flat list of trade records."""
    all_trades = []
    for yr in years:
        path = os.path.join(CACHE_DIR, f"house_{yr}.json")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            records = json.load(f)
        for rec in records:
            for t in rec.get("trades", []):
                tx = _parse_date(t["transaction_date"])
                fd = _parse_date(rec["filing_date"])
                if not tx or not fd:
                    continue
                lag = (fd - tx).days
                all_trades.append({
                    "chamber": "house",
                    "year": rec["year"],
                    "member": rec["member"],
                    "state": rec["state"],
                    "ticker": t["ticker"],
                    "asset_description": t.get("asset_description", ""),
                    "transaction_type": t["transaction_type"],
                    "transaction_date": t["transaction_date"],
                    "disclosure_date": rec["filing_date"],
                    "lag_days": lag,
                    "amount_range": t.get("amount_range", ""),
                    "owner": t.get("owner"),
                    "ptr_url": rec["pdf_url"],
                })
    combined_path = os.path.join(CACHE_DIR, "house_historical.json")
    with open(combined_path, "w") as f:
        json.dump(all_trades, f, indent=2)
    return combined_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--years", default="2022,2023,2024,2025",
                   help="Comma-separated years to backfill")
    p.add_argument("--workers", type=int, default=12,
                   help="Concurrent PDF downloads per year")
    p.add_argument("--combine-only", action="store_true",
                   help="Skip fetching; just combine existing per-year caches")
    args = p.parse_args()
    years = [int(y) for y in args.years.split(",") if y.strip()]

    if not args.combine_only:
        for yr in years:
            backfill_year(yr, args.workers)

    combined_path = _combine(years)
    with open(combined_path) as f:
        all_trades = json.load(f)

    print()
    print(f"=== Combined: {len(all_trades):,} trades across {len(years)} years ===")
    if all_trades:
        lags = [t["lag_days"] for t in all_trades]
        from statistics import mean, median
        print(f"  lag_days: mean {mean(lags):.1f}, median {median(lags)}, max {max(lags)}")
        for lo, hi in [(0, 10), (10, 20), (20, 30), (30, 45), (45, 1000)]:
            n = sum(1 for l in lags if lo <= l < hi)
            print(f"  lag {lo:>3}–{hi:<3}d: {n:>5} ({n/len(lags)*100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
