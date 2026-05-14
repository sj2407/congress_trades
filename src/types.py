from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


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
