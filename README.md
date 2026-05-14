# congress_trades

Daily alert for new congressional stock trades, with a flag when the trading
member sits on a committee whose jurisdiction touches the stock's sector.

## What it does

- Pulls latest disclosures from:
  - **Senate** — [senate-stock-watcher-data](https://github.com/timothycarambat/senate-stock-watcher-data) JSON aggregate
  - **House** — official [Clerk ZIP/XML index](https://disclosures-clerk.house.gov/) + per-PTR PDFs parsed with `pdfplumber`
- Loads current committee assignments from [unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators)
- Resolves each ticker's sector via `yfinance`
- Applies a committee↔sector conflict matrix (`src/conflicts.py`) → severity (high/low/none)
- Emails only the **new** trades since the last run (idempotent via local SQLite)

For each trade you see: chamber, member, ticker, type, amount range, **trade date**,
**disclosure date**, **lag in days**, sector, conflict reason, and the raw PTR link.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SMTP credentials
python main.py --dry-run    # writes data/preview.html, no email
python main.py              # sends email if there are new trades
```

For Gmail, create an **App Password** at https://myaccount.google.com/apppasswords
and use that as `SMTP_PASS`.

## One-off backtest

```bash
python backtest.py --limit 200            # quick run
python backtest.py                        # full history (slow)
```

Outputs `data/backtest.csv` with per-trade returns and a summary of how much of
the 30-day move had already happened by the time the trade was publicly disclosed.

## Editing the conflict matrix

`src/conflicts.py` maps committee-name fragments to sector/industry keywords.
Add to `COMMITTEE_JURISDICTION` to tighten or broaden the rules.

## Daily scheduling

Production runs as an Anthropic Claude Code scheduled task that:
1. clones this repo
2. installs deps from `requirements.txt`
3. runs `python main.py`
4. exits

SMTP credentials are supplied via the scheduled task's environment variables,
never committed.
