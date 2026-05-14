"""Price lookups via yfinance, with in-process per-ticker history caching.

For the daily email we need three price points per flagged trade:
  - price on transaction_date (insider entry)
  - price on disclosure_date (what the public could've bought at)
  - latest price (where it is now)

Each yfinance call is slow, so we fetch the full history per ticker ONCE and
serve all date lookups from the in-memory DataFrame.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

_HISTORY: dict[str, Optional[pd.DataFrame]] = {}


def _history(ticker: str) -> Optional[pd.DataFrame]:
    if ticker in _HISTORY:
        return _HISTORY[ticker]
    df: Optional[pd.DataFrame] = None
    try:
        df = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        if df is None or df.empty:
            df = None
        else:
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    except Exception:
        df = None
    _HISTORY[ticker] = df
    return df


def price_on_or_after(ticker: str, target: date, window_days: int = 7) -> Optional[float]:
    """Close on `target` or first trading day within `window_days` after."""
    if not ticker or not target:
        return None
    df = _history(ticker)
    if df is None or df.empty:
        return None
    target_ts = pd.Timestamp(target)
    sl = df.loc[(df.index >= target_ts) & (df.index <= target_ts + pd.Timedelta(days=window_days))]
    if sl.empty:
        return None
    return float(sl["Close"].iloc[0])


def latest_price(ticker: str) -> Optional[float]:
    df = _history(ticker)
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def latest_price_date(ticker: str) -> Optional[date]:
    df = _history(ticker)
    if df is None or df.empty:
        return None
    return df.index[-1].date()
