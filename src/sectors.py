"""Ticker -> (sector, industry) lookup via yfinance, with a small JSON disk cache.

yfinance scrapes Yahoo Finance; no API key required. Network calls are slow,
so we cache aggressively. Failures fall back to empty strings.
"""
from __future__ import annotations

import json
import os
from typing import Tuple

import yfinance as yf

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cache", "sectors.json")


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


_CACHE = _load_cache()


def lookup_sector(ticker: str) -> Tuple[str, str]:
    """Return (sector, industry) for a ticker, or ('', '') if unknown."""
    if not ticker:
        return ("", "")
    t = ticker.upper().strip()
    if t in _CACHE:
        c = _CACHE[t]
        return (c.get("sector", ""), c.get("industry", ""))
    sector, industry = "", ""
    try:
        info = yf.Ticker(t).info or {}
        sector = info.get("sector") or ""
        industry = info.get("industry") or ""
    except Exception:
        pass
    _CACHE[t] = {"sector": sector, "industry": industry}
    _save_cache(_CACHE)
    return (sector, industry)
