"""SQLite store of trades we've already emailed, so daily runs are idempotent."""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from typing import Iterable, List

from .types import Trade

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "seen.sqlite")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            uid TEXT PRIMARY KEY,
            chamber TEXT,
            member_name TEXT,
            ticker TEXT,
            transaction_type TEXT,
            transaction_date TEXT,
            disclosure_date TEXT,
            amount_range TEXT,
            seen_at TEXT
        )"""
    )
    return c


def filter_new(trades: Iterable[Trade]) -> List[Trade]:
    c = _conn()
    cur = c.cursor()
    new: List[Trade] = []
    for t in trades:
        uid = t.uid()
        cur.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,))
        if cur.fetchone() is None:
            new.append(t)
    c.close()
    return new


def mark_seen(trades: Iterable[Trade]) -> None:
    c = _conn()
    today = date.today().isoformat()
    for t in trades:
        c.execute(
            """INSERT OR IGNORE INTO seen
               (uid, chamber, member_name, ticker, transaction_type,
                transaction_date, disclosure_date, amount_range, seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.uid(),
                t.chamber,
                t.member_name,
                t.ticker or "",
                t.transaction_type,
                t.transaction_date.isoformat() if t.transaction_date else "",
                t.disclosure_date.isoformat(),
                t.amount_range,
                today,
            ),
        )
    c.commit()
    c.close()
