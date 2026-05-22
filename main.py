"""Daily orchestrator.

Run with:  python main.py [--dry-run] [--lookback-hours N]

Behavior:
- Fetches Senate (JSON) and House (Clerk ZIP + per-PTR PDF) trades.
- Filters to disclosures within LOOKBACK_HOURS (default 48).
- Filters to those NOT already in the local "seen" SQLite store.
- For each, looks up committees + ticker sector and flags conflicts.
- If any new trades remain, sends one HTML email and records them as seen.
- If none remain, exits quietly with no email (per user preference).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from src.committees import lookup_committees, lookup_member_info
from src.fetch_house import fetch_house_trades
from src.fetch_senate import fetch_senate_trades
from src.notify import render_email_html, send_email
from src.prices import latest_price, latest_price_date, price_on_or_after
from src.scoring import score_trade
from src.sectors import lookup_sector
from src.store import filter_new, mark_seen
from src.types import PositionStatus, PriceInfo


def _store_is_empty() -> bool:
    from src.store import _conn
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    c.close()
    return n == 0


def run(lookback_hours: int, dry_run: bool, house_year: int | None, max_house: int | None,
        preview_recent: int | None) -> int:
    cutoff = datetime.now() - timedelta(hours=lookback_hours)
    cutoff_date = cutoff.date()
    bootstrap = _store_is_empty()
    if bootstrap and not preview_recent:
        print("BOOTSTRAP: seen-store empty. Marking all existing trades as seen; no email this run.", flush=True)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Fetching Senate trades…", flush=True)
    senate = fetch_senate_trades()
    print(f"  senate: {len(senate)} total", flush=True)

    print(f"Fetching House trades for year {house_year or date.today().year}…", flush=True)
    house = fetch_house_trades(year=house_year, max_ptrs=max_house)
    print(f"  house: {len(house)} total", flush=True)

    all_trades = senate + house

    if preview_recent:
        # Preview mode: ignore seen-store, take the N most recent trades that
        # have actual ticker info (so the matrix has something to flag against).
        with_tk = [t for t in all_trades if t.ticker and t.transaction_date]
        with_tk.sort(key=lambda t: t.transaction_date, reverse=True)
        new_trades = with_tk[:preview_recent]
        print(f"  preview mode: showing {len(new_trades)} most recent trades with tickers", flush=True)
    else:
        if bootstrap:
            mark_seen(all_trades)
            print(f"Marked {len(all_trades)} trades as seen. Exiting.", flush=True)
            return 0

        recent = [t for t in all_trades if t.disclosure_date >= cutoff_date]
        print(f"  within last {lookback_hours}h: {len(recent)}", flush=True)

        # Public companies only: drop filings with no ticker (private holdings,
        # non-securities, and parse-failed placeholders with nothing extracted).
        public = [t for t in recent if t.ticker]
        if len(public) != len(recent):
            print(f"  dropped {len(recent) - len(public)} non-public / unparseable filing(s)", flush=True)

        new_trades = filter_new(public)
        print(f"  new (not yet seen): {len(new_trades)}", flush=True)

    if not new_trades:
        print("No new trades — skipping email.", flush=True)
        return 0

    # Index all trades (Senate + House) by (member, ticker) so we can detect
    # whether a flagged purchase has been closed by a later sale.
    def _norm(s: str) -> str:
        return " ".join(s.lower().split())
    later_sales: dict[tuple[str, str], list] = {}
    for t in all_trades:
        if not t.ticker or "ale" not in t.transaction_type.lower():
            continue
        later_sales.setdefault((_norm(t.member_name), t.ticker.upper()), []).append(t)

    enriched = []
    for t in new_trades:
        committees = lookup_committees(t.member_name)
        info = lookup_member_info(t.member_name)
        if not t.state and info.get("state"):
            t.state = info.get("state")
        if not t.party and info.get("party"):
            t.party = info.get("party")
        sector, industry = lookup_sector(t.ticker) if t.ticker else ("", "")
        score = score_trade(t, all_trades, committees, sector, industry)
        # Prices and position status are only meaningful when we know the ticker
        prices = PriceInfo()
        pos = PositionStatus()
        if t.ticker:
            if t.transaction_date:
                prices.at_trade = price_on_or_after(t.ticker, t.transaction_date)
            if t.disclosure_date:
                prices.at_disclosure = price_on_or_after(t.ticker, t.disclosure_date)
            prices.today = latest_price(t.ticker)
            prices.today_date = latest_price_date(t.ticker)
            # Position status: only meaningful for purchases
            if "urchase" in t.transaction_type.lower():
                key = (_norm(t.member_name), t.ticker.upper())
                later = [
                    s for s in later_sales.get(key, [])
                    if s.transaction_date and t.transaction_date
                    and s.transaction_date > t.transaction_date
                ]
                if later:
                    later.sort(key=lambda s: s.transaction_date)
                    close = later[0]
                    pos.state = "closed"
                    pos.closed_date = close.transaction_date
                    pos.closed_price = (
                        price_on_or_after(t.ticker, close.transaction_date)
                        if close.transaction_date else None
                    )
                else:
                    pos.state = "open"
            else:
                pos.state = ""  # not applicable for sales
        enriched.append((t, score, sector, industry, prices, pos))

    flagged = [x for x in enriched if x[1].tier != "none"]
    if not enriched and not preview_recent:
        print("  0 new disclosures — skipping email.", flush=True)
        return 0

    high = sum(1 for _, s, *_ in enriched if s.tier == "high")
    moderate = sum(1 for _, s, *_ in enriched if s.tier == "moderate")
    subject = (
        f"Congress trades — {len(enriched)} new"
        f"{f' ({high} high, {moderate} moderate)' if flagged else ' (none flagged)'}"
        f" — {date.today().isoformat()}"
    )
    body = render_email_html(enriched)

    if dry_run:
        out = os.path.join(os.path.dirname(__file__), "data", "preview.html")
        with open(out, "w") as f:
            f.write(body)
        print(f"DRY RUN — email preview written to {out}", flush=True)
        return 0

    send_email(subject, body)
    mark_seen(new_trades)
    print(f"Sent email with {len(enriched)} trades.", flush=True)
    return 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Write email to data/preview.html instead of sending")
    p.add_argument("--lookback-hours", type=int, default=int(os.environ.get("LOOKBACK_HOURS", "48")))
    p.add_argument("--house-year", type=int, default=None)
    p.add_argument("--max-house", type=int, default=None, help="Cap House PTRs (for testing)")
    p.add_argument("--preview-recent", type=int, default=None,
                   help="Bypass seen-filter and render the N most recent trades. "
                        "Use to inspect the email format and matrix output. Implies --dry-run.")
    args = p.parse_args()
    dry = args.dry_run or bool(args.preview_recent)
    return run(args.lookback_hours, dry, args.house_year, args.max_house, args.preview_recent)


if __name__ == "__main__":
    sys.exit(main())
