from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class PriceInfo:
    """Prices around a single trade, all closes or None when unavailable."""
    at_trade: Optional[float] = None
    at_disclosure: Optional[float] = None
    today: Optional[float] = None
    today_date: Optional[date] = None

    @property
    def gap_pct(self) -> Optional[float]:
        """Move from trade to disclosure (raw, not direction-adjusted)."""
        if self.at_trade and self.at_disclosure and self.at_trade != 0:
            return (self.at_disclosure - self.at_trade) / self.at_trade * 100
        return None

    @property
    def post_pct(self) -> Optional[float]:
        """Move from disclosure to latest close (raw)."""
        if self.at_disclosure and self.today and self.at_disclosure != 0:
            return (self.today - self.at_disclosure) / self.at_disclosure * 100
        return None


@dataclass
class PositionStatus:
    """Whether this position is closed/open as best we can tell from the dataset."""
    state: str = "unknown"      # "open", "closed", "unknown"
    closed_date: Optional[date] = None
    closed_price: Optional[float] = None


@dataclass
class Trade:
    chamber: str               # "house" or "senate"
    member_name: str           # canonical "First Last"
    state: Optional[str]       # 2-letter, when known
    party: Optional[str]
    ticker: Optional[str]      # may be None for non-stock assets
    asset_description: str
    asset_type: str            # "Stock", "Stock Option", "Other Securities", etc.
    transaction_type: str      # "Purchase", "Sale", "Exchange", etc.
    transaction_date: Optional[date]
    disclosure_date: date      # filing date — used for "what's new"
    amount_range: str          # e.g. "$1,001 - $15,000"
    owner: Optional[str] = None
    ptr_url: Optional[str] = None
    doc_id: Optional[str] = None

    @property
    def disclosure_lag_days(self) -> Optional[int]:
        if self.transaction_date and self.disclosure_date:
            return (self.disclosure_date - self.transaction_date).days
        return None

    def uid(self) -> str:
        # Stable id for dedupe: chamber|name|tx_date|ticker|tx_type|amount
        parts = [
            self.chamber,
            self.member_name.lower(),
            self.transaction_date.isoformat() if self.transaction_date else "",
            (self.ticker or "").upper(),
            self.transaction_type.lower(),
            self.amount_range,
        ]
        return "|".join(parts)
