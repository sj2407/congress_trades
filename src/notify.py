"""Compose and send the daily alert email via SMTP."""
from __future__ import annotations

import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Tuple

from .types import PositionStatus, PriceInfo, Trade

SEVERITY_RANK = {"high": 0, "moderate": 1, "low": 2, "none": 3}
SEVERITY_BADGE = {
    "high": ("🔴", "#b91c1c"),
    "moderate": ("🟠", "#c2410c"),
    "low": ("🟡", "#a16207"),
    "none": ("⚪", "#525252"),
}


def _money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    color = "#16a34a" if v >= 0 else "#dc2626"
    return f'<span style="color:{color};font-weight:600">{v:+.1f}%</span>'


def _position_cell(pos: PositionStatus) -> str:
    if not pos.state:
        return "—"
    if pos.state == "open":
        return '<span style="color:#16a34a">● Open</span>'
    if pos.state == "closed":
        d = pos.closed_date.isoformat() if pos.closed_date else "?"
        return f'<span style="color:#6b7280">○ Closed {d}</span>'
    return '<span style="color:#9ca3af">○ Unknown</span>'


def _row_html(
    t: Trade, severity: str, reasons: List[str], sector: str, industry: str,
    prices: PriceInfo, pos: PositionStatus,
) -> str:
    badge, color = SEVERITY_BADGE[severity]
    lag = t.disclosure_lag_days
    lag_str = f"{lag}d" if lag is not None else "—"
    ticker = t.ticker or "—"
    reasons_html = "<br>".join(html.escape(r) for r in reasons[:3]) if reasons else "—"
    ptr_link = f'<a href="{html.escape(t.ptr_url)}">PTR</a>' if t.ptr_url else "—"
    today_d = prices.today_date.isoformat() if prices.today_date else ""
    return f"""
    <tr>
      <td style="padding:6px 10px;vertical-align:top;color:{color};font-weight:600;white-space:nowrap">{badge} {severity}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.chamber.title())}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.member_name)}{f' ({html.escape(t.state)})' if t.state else ''}</td>
      <td style="padding:6px 10px;vertical-align:top;font-family:monospace;font-weight:600">{html.escape(ticker)}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.transaction_type or '—')}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.amount_range or '—')}</td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">
        {t.transaction_date or '—'}<br>
        <span style="color:#6b7280;font-size:12px">{_money(prices.at_trade)}</span>
      </td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">
        {t.disclosure_date}<br>
        <span style="color:#6b7280;font-size:12px">{_money(prices.at_disclosure)}</span>
      </td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right;white-space:nowrap">{lag_str}<br><span style="color:#6b7280;font-size:12px">private window</span></td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">{_pct(prices.gap_pct)}<br><span style="color:#6b7280;font-size:11px">trade → disc</span></td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">
        {_money(prices.today)}<br>
        <span style="color:#6b7280;font-size:11px">{today_d}</span>
      </td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">{_pct(prices.post_pct)}<br><span style="color:#6b7280;font-size:11px">disc → now</span></td>
      <td style="padding:6px 10px;vertical-align:top">{_position_cell(pos)}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(sector)}<br><span style="color:#6b7280;font-size:12px">{html.escape(industry)}</span></td>
      <td style="padding:6px 10px;vertical-align:top;font-size:12px">{reasons_html}</td>
      <td style="padding:6px 10px;vertical-align:top">{ptr_link}</td>
    </tr>
    """


def render_email_html(
    items: List[Tuple[Trade, str, List[str], str, str, PriceInfo, PositionStatus]],
) -> str:
    flagged = [x for x in items if x[1] != "none"]
    unflagged_count = len(items) - len(flagged)
    flagged_sorted = sorted(
        flagged,
        key=lambda x: (SEVERITY_RANK.get(x[1], 9), -(x[0].disclosure_date.toordinal())),
    )
    rows = "".join(_row_html(*x) for x in flagged_sorted)
    high_count = sum(1 for _, sev, *_ in flagged if sev == "high")
    mod_count = sum(1 for _, sev, *_ in flagged if sev == "moderate")
    low_count = sum(1 for _, sev, *_ in flagged if sev == "low")
    head = (
        f"<p><strong>{len(flagged)} flagged trade(s)</strong> — "
        f"🔴 {high_count} high · 🟠 {mod_count} moderate · 🟡 {low_count} low."
        f"<br><span style='color:#6b7280;font-size:13px'>"
        f"Plus {unflagged_count} other new trade(s) with no committee/sector overlap (not shown).</span></p>"
    )
    if not flagged:
        rows = '<tr><td colspan="16" style="padding:30px;text-align:center;color:#666">No committee-relevant conflicts in today\'s new trades.</td></tr>'
    return f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#111">
      <h2 style="margin-bottom:4px">Congress trades — daily digest</h2>
      {head}
      <p style="color:#6b7280;font-size:13px;margin-top:0">
        🔴 Member sits on a committee with direct jurisdiction over this stock's sector.
        🟡 Indirect overlap (e.g. trade policy). Position = whether this purchase has been
        closed by a later disclosed sale.
      </p>
      <table style="border-collapse:collapse;border:1px solid #ddd;font-size:13px;width:100%">
        <thead style="background:#f5f5f5">
          <tr>
            <th style="padding:6px 10px;text-align:left">Flag</th>
            <th style="padding:6px 10px;text-align:left">Chamber</th>
            <th style="padding:6px 10px;text-align:left">Member</th>
            <th style="padding:6px 10px;text-align:left">Ticker</th>
            <th style="padding:6px 10px;text-align:left">Type</th>
            <th style="padding:6px 10px;text-align:left">Amount</th>
            <th style="padding:6px 10px;text-align:right">Trade date / price</th>
            <th style="padding:6px 10px;text-align:right">Disclosure / price</th>
            <th style="padding:6px 10px;text-align:right">Lag</th>
            <th style="padding:6px 10px;text-align:right">Trade → disclosure</th>
            <th style="padding:6px 10px;text-align:right">Now</th>
            <th style="padding:6px 10px;text-align:right">Disclosure → now</th>
            <th style="padding:6px 10px;text-align:left">Position</th>
            <th style="padding:6px 10px;text-align:left">Sector / Industry</th>
            <th style="padding:6px 10px;text-align:left">Why flagged</th>
            <th style="padding:6px 10px;text-align:left">Source</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#6b7280;font-size:12px;margin-top:16px">
        Sources: official House Clerk disclosures + senate-stock-watcher.
        Conflict matrix: <code>src/conflicts.py</code>. Prices via yfinance.
      </p>
    </body></html>
    """


def send_email(subject: str, html_body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    sender = os.environ.get("ALERT_FROM", user)
    recipient = os.environ["ALERT_TO"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText("HTML email — view in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(sender, [recipient], msg.as_string())
