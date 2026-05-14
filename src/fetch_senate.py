"""Pull Senate trades from senate-stock-watcher-data on GitHub.

Source: https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json
Each record looks like:
    {
        "transaction_date": "11/10/2020",
        "owner": "Spouse",
        "ticker": "BYND",
        "asset_description": "Beyond Meat, Inc.",
        "asset_type": "Stock",
        "type": "Sale (Full)",
        "amount": "$50,001 - $100,000",
        "comment": "--",
        "senator": "Ron L Wyden",
        "ptr_link": "https://efdsearch.senate.gov/search/view/ptr/.../"
    }

The dataset does NOT carry a disclosure (filing) date per record. The PTR
filename in efdsearch URLs is opaque, so we approximate disclosure_date by the
date the record first appeared in the upstream JSON — captured via a local
"first-seen" cache keyed by record uid.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Iterable, List

import requests

from .types import Trade

SENATE_URL = (
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/"
    "master/aggregate/all_transactions.json"
)
FIRST_SEEN_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "cache", "senate_first_seen.json"
)


def _parse_date(s: str | None) -> date | None:
    if not s or s in {"--", ""}:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _load_first_seen() -> dict:
    try:
        with open(FIRST_SEEN_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_first_seen(d: dict) -> None:
    os.makedirs(os.path.dirname(FIRST_SEEN_PATH), exist_ok=True)
    with open(FIRST_SEEN_PATH, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


def fetch_senate_trades() -> List[Trade]:
    r = requests.get(SENATE_URL, timeout=60)
    r.raise_for_status()
    raw = r.json()

    first_seen = _load_first_seen()
    today_iso = date.today().isoformat()

    trades: List[Trade] = []
    for rec in raw:
        tx_date = _parse_date(rec.get("transaction_date"))
        ticker = (rec.get("ticker") or "").strip()
        if ticker in {"--", ""}:
            ticker = None

        t = Trade(
            chamber="senate",
            member_name=rec.get("senator", "").strip(),
            state=None,
            party=None,
            ticker=ticker.upper() if ticker else None,
            asset_description=rec.get("asset_description", "").strip(),
            asset_type=(rec.get("asset_type") or "").strip(),
            transaction_type=(rec.get("type") or "").strip(),
            transaction_date=tx_date,
            disclosure_date=date.today(),  # placeholder, overridden below
            amount_range=(rec.get("amount") or "").strip(),
            owner=(rec.get("owner") or None),
            ptr_url=rec.get("ptr_link"),
        )
        uid = t.uid()
        seen_iso = first_seen.get(uid)
        if seen_iso is None:
            first_seen[uid] = today_iso
            seen_iso = today_iso
        try:
            t.disclosure_date = date.fromisoformat(seen_iso)
        except ValueError:
            t.disclosure_date = date.today()
        trades.append(t)

    _save_first_seen(first_seen)
    return trades
