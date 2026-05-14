"""Pull House PTR (Periodic Transaction Report) filings from the official
House Clerk disclosure site.

Sources:
- Annual index ZIP: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip
  contains {YEAR}FD.xml listing every filing (PTR, AR, etc.)
- Per-filing PDF: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf

We:
1. Download + parse the XML index, keep FilingType=='P' (PTR) records.
2. For each PTR, fetch the PDF, parse out transactions with pdfplumber.
3. Best-effort: if a PDF can't be parsed, emit a single Trade record marked
   "parse-failed" with the PDF URL so the email still surfaces the filing.

PDF schema is a table with columns roughly:
  #  Owner  Asset  Transaction Type  Date  Notification Date  Amount
Tickers appear inside the Asset cell as "(TICKER)".
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

import pdfplumber
import requests

from .types import Trade

CLERK_ZIP_TMPL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
CLERK_PDF_TMPL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"

TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,5})\)")
DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
AMOUNT_RE = re.compile(
    r"\$[\d,]+(?:\s*-\s*\$[\d,]+|\s*-\s*\$?[\d,]+)?", re.IGNORECASE
)
TX_TYPE_RE = re.compile(r"\b(P|S|S \(partial\)|E|Purchase|Sale|Sale \(Partial\)|Exchange)\b", re.IGNORECASE)

TX_TYPE_MAP = {
    "P": "Purchase",
    "S": "Sale (Full)",
    "S (PARTIAL)": "Sale (Partial)",
    "S (Partial)": "Sale (Partial)",
    "E": "Exchange",
    "PURCHASE": "Purchase",
    "SALE": "Sale (Full)",
    "SALE (PARTIAL)": "Sale (Partial)",
    "EXCHANGE": "Exchange",
}


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fetch_house_index(year: int) -> List[dict]:
    """Return list of PTR index records for the year.

    Each record: {doc_id, year, first, last, state_dst, filing_date_iso}
    """
    url = CLERK_ZIP_TMPL.format(year=year)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml_name = next((n for n in z.namelist() if n.endswith(".xml")), None)
        if not xml_name:
            return []
        xml_bytes = z.read(xml_name)

    root = ET.fromstring(xml_bytes)
    out: List[dict] = []
    for m in root.findall(".//Member"):
        filing_type = (m.findtext("FilingType") or "").strip()
        if filing_type != "P":
            continue
        fd = _parse_date(m.findtext("FilingDate") or "")
        out.append({
            "doc_id": (m.findtext("DocID") or "").strip(),
            "year": int((m.findtext("Year") or year)),
            "prefix": (m.findtext("Prefix") or "").strip(),
            "first": (m.findtext("First") or "").strip(),
            "last": (m.findtext("Last") or "").strip(),
            "suffix": (m.findtext("Suffix") or "").strip(),
            "state_dst": (m.findtext("StateDst") or "").strip(),
            "filing_date": fd,
        })
    return out


def _download_pdf(year: int, doc_id: str) -> Optional[bytes]:
    url = CLERK_PDF_TMPL.format(year=year, doc=doc_id)
    try:
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            return None
        return r.content
    except requests.RequestException:
        return None


def _parse_ptr_pdf(pdf_bytes: bytes) -> List[Tuple[Optional[str], str, str, Optional[date], Optional[date], str, Optional[str]]]:
    """Return list of (ticker, asset_desc, tx_type, tx_date, notif_date, amount, owner).

    Best-effort. Returns [] if structure can't be detected.
    """
    rows: List[Tuple] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                # 1) try table extraction
                for table in page.extract_tables() or []:
                    if not table or len(table) < 2:
                        continue
                    header = [(c or "").strip().lower() for c in table[0]]
                    # Heuristic: a PTR table has "asset" and "amount" columns
                    if not any("asset" in h for h in header):
                        continue
                    col = {h: i for i, h in enumerate(header)}
                    def cell(row, *keys):
                        for k in keys:
                            for h, i in col.items():
                                if k in h and i < len(row):
                                    return (row[i] or "").strip()
                        return ""
                    for row in table[1:]:
                        asset = cell(row, "asset")
                        tx = cell(row, "transaction type", "type")
                        d = cell(row, "transaction date", "date")
                        nd = cell(row, "notification date", "notif")
                        amt = cell(row, "amount")
                        owner = cell(row, "owner")
                        # Real PTR rows always carry an amount range like "$1,001 -"
                        if "$" not in (amt or ""):
                            continue
                        m = TICKER_RE.search(asset or "")
                        ticker = m.group(1) if m else None
                        asset_clean = TICKER_RE.sub("", asset or "").replace("\n", " ").strip(" -")
                        tx_norm = TX_TYPE_MAP.get(tx.upper(), tx)
                        rows.append((
                            ticker,
                            asset_clean,
                            tx_norm,
                            _parse_date(d),
                            _parse_date(nd),
                            amt,
                            owner or None,
                        ))
                if rows:
                    continue
                # 2) fall back to line-based scan for tabular text
                text = page.extract_text() or ""
                for line in text.splitlines():
                    if "(" not in line or "$" not in line:
                        continue
                    m = TICKER_RE.search(line)
                    if not m:
                        continue
                    ticker = m.group(1)
                    amt_m = AMOUNT_RE.search(line)
                    dt_m = DATE_RE.search(line)
                    tx_m = TX_TYPE_RE.search(line)
                    rows.append((
                        ticker,
                        TICKER_RE.sub("", line).strip()[:120],
                        TX_TYPE_MAP.get((tx_m.group(1).upper() if tx_m else ""), tx_m.group(1) if tx_m else ""),
                        _parse_date(dt_m.group(1)) if dt_m else None,
                        None,
                        amt_m.group(0) if amt_m else "",
                        None,
                    ))
    except Exception:
        return []
    return rows


def fetch_house_trades(year: Optional[int] = None, max_ptrs: Optional[int] = None) -> List[Trade]:
    """Fetch + parse House PTRs for the given year (default: current year)."""
    if year is None:
        year = date.today().year
    index = fetch_house_index(year)
    if max_ptrs is not None:
        index = index[:max_ptrs]
    trades: List[Trade] = []
    for entry in index:
        member_name = " ".join(x for x in [entry["first"], entry["last"]] if x).strip()
        state = entry["state_dst"][:2] if entry["state_dst"] else None
        pdf_url = CLERK_PDF_TMPL.format(year=entry["year"], doc=entry["doc_id"])
        pdf_bytes = _download_pdf(entry["year"], entry["doc_id"])
        parsed = _parse_ptr_pdf(pdf_bytes) if pdf_bytes else []
        if not parsed:
            # Emit a single placeholder trade so the email still surfaces this filing
            trades.append(Trade(
                chamber="house",
                member_name=member_name,
                state=state,
                party=None,
                ticker=None,
                asset_description="(PDF parse failed — see PTR link)",
                asset_type="",
                transaction_type="",
                transaction_date=None,
                disclosure_date=entry["filing_date"] or date.today(),
                amount_range="",
                owner=None,
                ptr_url=pdf_url,
                doc_id=entry["doc_id"],
            ))
            continue
        for ticker, asset, tx_type, tx_date, _notif, amt, owner in parsed:
            trades.append(Trade(
                chamber="house",
                member_name=member_name,
                state=state,
                party=None,
                ticker=ticker,
                asset_description=asset,
                asset_type="",
                transaction_type=tx_type or "",
                transaction_date=tx_date,
                disclosure_date=entry["filing_date"] or date.today(),
                amount_range=amt or "",
                owner=owner,
                ptr_url=pdf_url,
                doc_id=entry["doc_id"],
            ))
    return trades
