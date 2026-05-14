"""Compose and send the daily alert email via SMTP."""
from __future__ import annotations

import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Tuple

from .types import Trade

SEVERITY_RANK = {"high": 0, "moderate": 1, "low": 2, "none": 3}
SEVERITY_BADGE = {
    "high": ("🔴", "#b91c1c"),
    "moderate": ("🟠", "#c2410c"),
    "low": ("🟡", "#a16207"),
    "none": ("⚪", "#525252"),
}


def _row_html(t: Trade, severity: str, reasons: List[str], sector: str, industry: str) -> str:
    badge, color = SEVERITY_BADGE[severity]
    lag = t.disclosure_lag_days
    lag_str = f"{lag}d gap" if lag is not None else "—"
    ticker = t.ticker or "—"
    reasons_html = "<br>".join(html.escape(r) for r in reasons) if reasons else "—"
    ptr_link = (
        f'<a href="{html.escape(t.ptr_url)}">PTR</a>' if t.ptr_url else "—"
    )
    return f"""
    <tr>
      <td style="padding:6px 10px;vertical-align:top;color:{color};font-weight:600;white-space:nowrap">{badge} {severity}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.chamber.title())}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.member_name)}</td>
      <td style="padding:6px 10px;vertical-align:top;font-family:monospace">{html.escape(ticker)}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.transaction_type or '—')}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.amount_range or '—')}</td>
      <td style="padding:6px 10px;vertical-align:top">{t.transaction_date or '—'}</td>
      <td style="padding:6px 10px;vertical-align:top">{t.disclosure_date}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(lag_str)}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(sector)}<br><span style="color:#666;font-size:12px">{html.escape(industry)}</span></td>
      <td style="padding:6px 10px;vertical-align:top;font-size:12px">{reasons_html}</td>
      <td style="padding:6px 10px;vertical-align:top">{ptr_link}</td>
    </tr>
    """


def render_email_html(
    items: List[Tuple[Trade, str, List[str], str, str]],
) -> str:
    items_sorted = sorted(
        items,
        key=lambda x: (SEVERITY_RANK.get(x[1], 9), -(x[0].disclosure_date.toordinal())),
    )
    rows = "".join(_row_html(t, sev, r, s, i) for t, sev, r, s, i in items_sorted)
    high_count = sum(1 for _, sev, *_ in items if sev == "high")
    head = f"<p><strong>{len(items)} new trade(s)</strong> — {high_count} flagged as committee-relevant conflicts.</p>"
    return f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#111">
      <h2 style="margin-bottom:4px">Congress trades — daily digest</h2>
      {head}
      <table style="border-collapse:collapse;border:1px solid #ddd;font-size:13px">
        <thead style="background:#f5f5f5">
          <tr>
            <th style="padding:6px 10px;text-align:left">Flag</th>
            <th style="padding:6px 10px;text-align:left">Chamber</th>
            <th style="padding:6px 10px;text-align:left">Member</th>
            <th style="padding:6px 10px;text-align:left">Ticker</th>
            <th style="padding:6px 10px;text-align:left">Type</th>
            <th style="padding:6px 10px;text-align:left">Amount</th>
            <th style="padding:6px 10px;text-align:left">Trade date</th>
            <th style="padding:6px 10px;text-align:left">Disclosed</th>
            <th style="padding:6px 10px;text-align:left">Lag</th>
            <th style="padding:6px 10px;text-align:left">Sector / Industry</th>
            <th style="padding:6px 10px;text-align:left">Conflict</th>
            <th style="padding:6px 10px;text-align:left">Source</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#666;font-size:12px;margin-top:16px">
        Source: official House Clerk disclosures + senate-stock-watcher.
        Conflict matrix in <code>src/conflicts.py</code>; edit to refine.
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
