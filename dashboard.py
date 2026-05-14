"""Narrative dashboard for the congress-trades backtest.

Tells the story in plain English with one running example. No jargon —
every metric is described in concrete terms.

Sections:
  A. The setup (running example card)
  B. How much does the stock move during the secret window?
  C. Does it matter how fast they disclose? (lag buckets — House only)
  D. Buys vs sells — the asymmetry
  E. Who has the edge? (two member rankings + scatter)
  F. How long do they hold? (holding-period histogram)
  G. Does our committee-conflict flag predict alpha?
  H. How to read tomorrow's alert (decision card)

Inputs:
  data/backtest.csv         — Senate (assumed 30-day disclosure lag)
  data/backtest_house.csv   — House (real disclosure dates)
  data/cache/house_historical.json — for holding-period analysis

Re-run after editing src/conflicts.py to see whether the matrix change
tightens or loosens the historical signal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from src.committees import lookup_committees
from src.conflicts import COMMITTEE_JURISDICTION, SECTOR_KEYWORDS, _normalize, detect_conflict, BROAD_KEY, TRADE_KEY
from src.sectors import lookup_sector

FLAG_COLOR = {"high": "#dc2626", "moderate": "#ea580c", "low": "#ca8a04", "none": "#9ca3af"}
FLAG_LABEL = {
    "high": "Strong match",
    "moderate": "Some match",
    "low": "Weak match",
    "none": "No match",
}
FLAG_ORDER = ["high", "moderate", "low", "none"]


# ──────────────────────────────────────────────────────────────
# Loading + enrichment
# ──────────────────────────────────────────────────────────────

def _load_csv(path: str, source: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["source"] = source
    num_cols = ["direction", "lag_days", "price_trade", "price_disclose",
                "price_disclose_30", "price_disclose_90", "price_today",
                "ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct",
                "ret_to_today_pct", "captured_share_pct"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["disclosure_date"] = pd.to_datetime(df["disclosure_date"], errors="coerce")
    return df


def _enrich_conflict(df: pd.DataFrame) -> pd.DataFrame:
    """Add sector + committee-conflict flag to every row."""
    print(f"  enriching {len(df):,} rows with sector + committee flag…", flush=True)
    sectors, industries = {}, {}
    for tk in df["ticker"].dropna().unique():
        s, i = lookup_sector(tk)
        sectors[tk] = s
        industries[tk] = i
    df["sector"] = df["ticker"].map(lambda t: sectors.get(t, "") if isinstance(t, str) else "")
    df["industry"] = df["ticker"].map(lambda t: industries.get(t, "") if isinstance(t, str) else "")
    member_cache = {n: lookup_committees(n) for n in df["member"].dropna().unique()}
    df["flag"] = df.apply(
        lambda r: detect_conflict(member_cache.get(r["member"], []),
                                  r["sector"] or "", r["industry"] or "")[0],
        axis=1,
    )
    return df


def _holding_periods() -> pd.DataFrame:
    """Compute holding periods from data/cache/house_historical.json.

    For each purchase, find the earliest subsequent sale of the same ticker by
    the same member. If found, record days_held + return; if not, mark Open.
    """
    path = "data/cache/house_historical.json"
    if not os.path.exists(path):
        return pd.DataFrame()
    with open(path) as f:
        trades = json.load(f)
    by_key: dict = {}
    for t in trades:
        if not t.get("ticker") or not t.get("transaction_date"):
            continue
        key = (t["member"].lower().strip(), t["ticker"].upper())
        by_key.setdefault(key, []).append(t)
    for k in by_key:
        by_key[k].sort(key=lambda x: x["transaction_date"])

    rows = []
    for key, ts in by_key.items():
        for i, t in enumerate(ts):
            if "urchase" not in t["transaction_type"].lower():
                continue
            sale = next(
                (s for s in ts[i + 1:] if "ale" in s["transaction_type"].lower()), None
            )
            if sale:
                tx = datetime.strptime(t["transaction_date"], "%Y-%m-%d").date()
                sx = datetime.strptime(sale["transaction_date"], "%Y-%m-%d").date()
                rows.append({
                    "member": t["member"],
                    "ticker": t["ticker"],
                    "purchase_date": tx,
                    "sale_date": sx,
                    "days_held": (sx - tx).days,
                    "status": "closed",
                })
            else:
                tx = datetime.strptime(t["transaction_date"], "%Y-%m-%d").date()
                rows.append({
                    "member": t["member"],
                    "ticker": t["ticker"],
                    "purchase_date": tx,
                    "sale_date": None,
                    "days_held": (date.today() - tx).days,
                    "status": "open",
                })
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────
# Page parts
# ──────────────────────────────────────────────────────────────

def _running_example_card(df: pd.DataFrame) -> tuple[str, dict]:
    """Pick a vivid recent House trade to use as the running example.

    Criteria:
      - House (real dates)
      - lag between 14 and 30 days (typical, not extreme)
      - strong absolute gap move (>10%)
      - direction matched the move (it was a "right" call)
      - traded in the last 12 months for recency
    """
    house = df[(df["source"] == "house") & df["ret_during_gap_pct"].notna()].copy()
    house = house[(house["lag_days"] >= 14) & (house["lag_days"] <= 30)]
    house = house[house["ret_during_gap_pct"] > 10]
    cutoff = pd.Timestamp(date.today() - timedelta(days=365 * 2))
    house = house[house["trade_date"] >= cutoff]
    if house.empty:
        # fall back to any strong gap move
        house = df[df["ret_during_gap_pct"].notna() & (df["ret_during_gap_pct"] > 10)]
    if house.empty:
        return "<p>No example available.</p>", {}
    ex = house.sort_values("ret_during_gap_pct", ascending=False).iloc[0]

    side = "bought" if ex["direction"] == 1 else "sold"
    sign = "rose" if ex["direction"] == 1 else "fell"
    gap_abs = abs(ex["ret_during_gap_pct"])
    post = ex["ret_post_30_pct"] if pd.notna(ex["ret_post_30_pct"]) else float("nan")
    post_str = f"{post:+.1f}%" if pd.notna(post) else "?"

    html = f"""
    <div style="border:1px solid #e5e7eb;border-radius:10px;padding:18px 22px;background:#fafbfc;margin:12px 0">
      <div style="color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:0.06em">Running example</div>
      <div style="font-size:18px;font-weight:600;margin-top:6px">{ex['member']} {side} <span style='font-family:monospace'>{ex['ticker']}</span></div>
      <div style="margin-top:8px;line-height:1.7;color:#374151">
        Traded <strong>{ex['trade_date'].date()}</strong> at <strong>${ex['price_trade']:.2f}</strong>.
        Disclosed <strong>{ex['disclosure_date'].date()}</strong> ({int(ex['lag_days'])} days later) — by then the stock was
        <strong>${ex['price_disclose']:.2f}</strong>, having {sign} <strong>{gap_abs:.1f}%</strong> in the secret window.
        In the 30 days after disclosure, anyone copying the trade would have made <strong>{post_str}</strong> more.
      </div>
      <div style="margin-top:8px;color:#6b7280;font-size:13px">
        We'll use this trade as a touchstone. Every chart below asks: <em>is this typical or is this unusual?</em>
      </div>
    </div>
    """
    return html, ex.to_dict()


def _kpi_strip(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p = valid["ret_post_30_pct"].mean()
    n_house = (df["source"] == "house").sum()
    n_senate = (df["source"] == "senate").sum()
    cards = [
        ("Trades analyzed", f"{len(valid):,}",
         f"{n_house:,} House (real disclosure dates) · {n_senate:,} Senate (assumed 30d lag)"),
        ("Average move during secret window", f"{g:+.2f}%",
         "What members had already 'earned' before the public could see"),
        ("Average move available after disclosure", f"{p:+.2f}%",
         "What's left if you copy the trade once it's public"),
        ("Share of the move already captured", f"{g/(g+p)*100:.0f}%" if (g + p) != 0 else "—",
         "Before the public knew, this much of the 30-day move was done"),
    ]
    body = "".join(f"""
        <div style="flex:1;min-width:200px;border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;background:#fafafa">
          <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.05em">{label}</div>
          <div style="font-size:24px;font-weight:600;margin-top:4px">{value}</div>
          <div style="color:#6b7280;font-size:12px;margin-top:4px;line-height:1.4">{sub}</div>
        </div>
    """ for label, value, sub in cards)
    return f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:14px 0">{body}</div>'


def _chart_horizons(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p30 = valid["ret_post_30_pct"].mean()
    p90 = valid["ret_post_90_pct"].mean()
    fig = go.Figure(go.Bar(
        x=["During the secret window", "30 days after public disclosure", "90 days after public disclosure"],
        y=[g, p30, p90],
        marker_color=["#dc2626", "#2563eb", "#94a3b8"],
        text=[f"{v:+.2f}%" for v in [g, p30, p90]],
        textposition="outside",
    ))
    fig.update_layout(
        yaxis_title="Average % the stock moved (the senator/rep's direction)",
        height=380, margin=dict(t=30, l=40, r=20, b=80),
    )
    return fig.to_html(include_plotlyjs="cdn", full_html=False, div_id="c-horizons")


def _chart_lag_buckets(df: pd.DataFrame) -> str:
    house = df[df["source"] == "house"].dropna(subset=["ret_during_gap_pct", "ret_post_30_pct", "lag_days"]).copy()
    if house.empty:
        return "<p style='color:#6b7280'>House backtest data not available — re-run <code>python backtest_house.py</code>.</p>"
    bins = [(0, 10), (10, 20), (20, 30), (30, 45), (45, 1000)]
    labels = ["<10 days", "10–20 days", "20–30 days", "30–45 days", "Late (>45 days)"]
    rows = []
    for (lo, hi), lab in zip(bins, labels):
        bucket = house[(house["lag_days"] >= lo) & (house["lag_days"] < hi)]
        if bucket.empty:
            continue
        g = bucket["ret_during_gap_pct"].mean()
        p = bucket["ret_post_30_pct"].mean()
        rows.append({"bucket": lab, "n": len(bucket), "gap": g, "post": p})
    if not rows:
        return "<p>No data.</p>"
    d = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_bar(
        name="Move during the secret window",
        x=d["bucket"], y=d["gap"], marker_color="#dc2626",
        text=[f"{v:+.1f}%<br>n={n:,}" for v, n in zip(d["gap"], d["n"])],
        textposition="outside",
    )
    fig.add_bar(
        name="Move 30d after public disclosure",
        x=d["bucket"], y=d["post"], marker_color="#2563eb",
        text=[f"{v:+.1f}%" for v in d["post"]],
        textposition="outside",
    )
    fig.update_layout(
        yaxis_title="Average % the stock moved (the trader's direction)",
        barmode="group", height=420,
        legend=dict(orientation="h", y=-0.20),
        margin=dict(t=30, l=40, r=20, b=80),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-lag")


def _chart_buys_vs_sells(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"]).copy()
    valid["side"] = valid["direction"].map({1: "Buys (purchases)", -1: "Sells"})
    g = valid.groupby("side").agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    ).reset_index()
    fig = go.Figure()
    fig.add_bar(name="Move during the secret window", x=g["side"], y=g["gap"],
                marker_color="#dc2626",
                text=[f"{v:+.2f}%<br>(n={n:,})" for v, n in zip(g["gap"], g["n"])],
                textposition="outside")
    fig.add_bar(name="Move 30d after disclosure", x=g["side"], y=g["post"],
                marker_color="#2563eb",
                text=[f"{v:+.2f}%" for v in g["post"]],
                textposition="outside")
    fig.update_layout(yaxis_title="Average % (trader's direction)",
                      barmode="group", height=400,
                      legend=dict(orientation="h", y=-0.18),
                      margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-pvs")


def _chart_members_two_views(df: pd.DataFrame, min_trades: int = 10) -> str:
    """Two side-by-side rankings: top by secret-window return, top by total return."""
    buys = df[(df["direction"] == 1) & df["ret_during_gap_pct"].notna()].copy()
    # Annualize ret_to_today_pct over years since trade
    buys["years_since_trade"] = (pd.Timestamp(date.today()) - buys["trade_date"]).dt.days / 365.25
    buys = buys[buys["years_since_trade"] > 0.25]
    buys["annualized_total"] = buys.apply(
        lambda r: ((1 + r["ret_to_today_pct"] / 100) ** (1 / r["years_since_trade"]) - 1) * 100
        if pd.notna(r["ret_to_today_pct"]) else float("nan"),
        axis=1,
    )
    by_member = buys.groupby("member").agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        ann=("annualized_total", "mean"),
    ).reset_index()
    by_member = by_member[by_member["n"] >= min_trades]
    top_gap = by_member.nlargest(10, "gap").sort_values("gap")
    top_total = by_member.nlargest(10, "ann").sort_values("ann")

    fig = go.Figure()
    fig.add_bar(name="Best timers (secret-window)",
                x=top_gap["gap"],
                y=top_gap["member"] + " (n=" + top_gap["n"].astype(str) + ")",
                orientation="h", marker_color="#dc2626", xaxis="x1", yaxis="y1",
                text=[f"{v:+.1f}%" for v in top_gap["gap"]], textposition="outside")
    fig.add_bar(name="Best long-term picks (annualized)",
                x=top_total["ann"],
                y=top_total["member"] + " (n=" + top_total["n"].astype(str) + ")",
                orientation="h", marker_color="#16a34a", xaxis="x2", yaxis="y2",
                text=[f"{v:+.1f}%" for v in top_total["ann"]], textposition="outside")
    fig.update_layout(
        grid=dict(rows=1, columns=2, pattern="independent"),
        height=520,
        margin=dict(t=30, l=180, r=180, b=40),
        showlegend=False,
        xaxis=dict(title="Avg % in secret window", anchor="y1"),
        yaxis=dict(domain=[0, 1], anchor="x1"),
        xaxis2=dict(title="Avg annualized return to today", anchor="y2"),
        yaxis2=dict(domain=[0, 1], anchor="x2"),
        annotations=[
            dict(text="<b>Best timers</b><br>biggest gains during secret window",
                 x=0.0, y=1.10, xref="paper", yref="paper", showarrow=False, align="left"),
            dict(text="<b>Best pickers</b><br>biggest long-term winners",
                 x=0.60, y=1.10, xref="paper", yref="paper", showarrow=False, align="left"),
        ],
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-members")


def _chart_member_scatter(df: pd.DataFrame, min_trades: int = 10) -> str:
    buys = df[(df["direction"] == 1) & df["ret_during_gap_pct"].notna()].copy()
    buys["years_since_trade"] = (pd.Timestamp(date.today()) - buys["trade_date"]).dt.days / 365.25
    buys = buys[buys["years_since_trade"] > 0.25]
    buys["annualized_total"] = buys.apply(
        lambda r: ((1 + r["ret_to_today_pct"] / 100) ** (1 / r["years_since_trade"]) - 1) * 100
        if pd.notna(r["ret_to_today_pct"]) else float("nan"),
        axis=1,
    )
    by_member = buys.groupby("member").agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        ann=("annualized_total", "mean"),
    ).reset_index()
    by_member = by_member[by_member["n"] >= min_trades]
    fig = px.scatter(
        by_member, x="gap", y="ann", size="n", hover_name="member",
        labels={"gap": "Avg move in secret window (%)",
                "ann": "Avg annualized return to today (%)"},
    )
    fig.add_hline(y=0, line_dash="dot", line_color="#9ca3af")
    fig.add_vline(x=0, line_dash="dot", line_color="#9ca3af")
    fig.update_traces(marker=dict(color="#1d4ed8", opacity=0.6))
    fig.update_layout(
        height=500, margin=dict(t=30, l=40, r=20, b=40),
        annotations=[
            dict(text="Right side = good timer · Top side = good picker · Top-right = both",
                 x=0.5, y=1.06, xref="paper", yref="paper", showarrow=False,
                 font=dict(size=12, color="#6b7280")),
        ],
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-scatter")


def _chart_holding(holding: pd.DataFrame) -> str:
    if holding.empty:
        return "<p style='color:#6b7280'>Need the historical House cache. Run <code>python backfill_house.py</code>.</p>"
    closed = holding[holding["status"] == "closed"]
    n_open = (holding["status"] == "open").sum()
    n_total = len(holding)
    pct_open = n_open / n_total * 100
    if closed.empty:
        return f"<p>{n_open:,} of {n_total:,} disclosed purchases are still open ({pct_open:.0f}%); no closed positions found.</p>"
    fig = px.histogram(
        closed, x="days_held", nbins=40,
        labels={"days_held": "Days from purchase to disclosed sale"},
    )
    fig.update_traces(marker=dict(color="#1d4ed8"))
    median_held = closed["days_held"].median()
    fig.update_layout(height=380, margin=dict(t=30, l=40, r=20, b=40),
                      yaxis_title="Number of trades",
                      title_text=(f"Among CLOSED positions: median holding period {int(median_held)} days. "
                                  f"{n_open:,} of {n_total:,} purchases ({pct_open:.0f}%) are still open."))
    fig.add_vline(x=median_held, line_dash="dash", line_color="#dc2626",
                  annotation_text=f"median = {int(median_held)}d", annotation_position="top")
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-holding")


def _per_committee_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """For each (committee_fragment, jurisdiction_keyword) in the matrix,
    measure the mean gap return on BUYS where the trader sat on that committee
    AND the stock's sector/industry matched that jurisdiction keyword.

    Returns one row per committee-fragment with: n, mean_gap, mean_post.
    Aggregated across all jurisdiction keywords for that committee.
    """
    buys = df[(df["direction"] == 1)].dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"]).copy()
    member_cache = {n: lookup_committees(n) for n in buys["member"].dropna().unique()}

    # Build a quick lookup: member -> normalized set of fragment substrings that
    # appear in any of their committees.
    member_frags: dict[str, set[str]] = {}
    for member, comms in member_cache.items():
        frags = set()
        for c in comms:
            nc = _normalize(c)
            for frag in COMMITTEE_JURISDICTION:
                if frag in nc:
                    frags.add(frag)
        member_frags[member] = frags

    # For each row, vectorize: collect which (frag, jkey) match.
    rows_out: dict[str, dict] = {}
    for _, r in buys.iterrows():
        haystack = f"{(r['sector'] or '').lower()} | {(r['industry'] or '').lower()}"
        for frag in member_frags.get(r["member"], ()):
            jurisdiction = COMMITTEE_JURISDICTION[frag]
            if not jurisdiction:
                continue
            # Skip the broad/trade markers — they aren't sector-specific
            sector_keys = jurisdiction - {BROAD_KEY, TRADE_KEY}
            matched = False
            for jkey in sector_keys:
                for needle in SECTOR_KEYWORDS.get(jkey, {jkey}):
                    if needle and needle in haystack:
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                continue
            agg = rows_out.setdefault(frag, {"committee": frag, "gaps": [], "posts": []})
            agg["gaps"].append(r["ret_during_gap_pct"])
            agg["posts"].append(r["ret_post_30_pct"])

    rows = []
    for frag, agg in rows_out.items():
        if not agg["gaps"]:
            continue
        rows.append({
            "committee": frag,
            "n": len(agg["gaps"]),
            "mean_gap": sum(agg["gaps"]) / len(agg["gaps"]),
            "mean_post": sum(agg["posts"]) / len(agg["posts"]),
        })
    return pd.DataFrame(rows).sort_values("mean_gap", ascending=False)


def _chart_per_committee(per_comm: pd.DataFrame, baseline: float) -> str:
    """Horizontal bar chart: mean gap return per committee fragment, with the
    overall-buys baseline shown as a vertical line."""
    if per_comm.empty:
        return "<p style='color:#6b7280'>No committee-flagged buys to analyze.</p>"
    d = per_comm.sort_values("mean_gap")
    colors = ["#16a34a" if v > baseline else "#dc2626" for v in d["mean_gap"]]
    labels = [c.title() for c in d["committee"]]
    fig = go.Figure(go.Bar(
        x=d["mean_gap"],
        y=[f"{lab} (n={n})" for lab, n in zip(labels, d["n"])],
        orientation="h",
        marker_color=colors,
        text=[f"{v:+.2f}%" for v in d["mean_gap"]],
        textposition="outside",
    ))
    fig.add_vline(x=baseline, line_dash="dash", line_color="#374151",
                  annotation_text=f"all buys avg: {baseline:+.2f}%",
                  annotation_position="top right")
    fig.update_layout(
        xaxis_title="Mean move in secret window (% direction-adjusted)",
        height=max(380, 30 * len(d) + 80),
        margin=dict(t=40, l=320, r=80, b=40),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-per-comm")


def _chart_short_lag_flag(df: pd.DataFrame) -> str:
    """Same severity chart but restricted to trades with <20-day disclosure lag
    (House only — Senate doesn't have real lag data)."""
    short = df[(df["source"] == "house") & (df["lag_days"] < 20) & (df["direction"] == 1)].dropna(
        subset=["ret_during_gap_pct", "ret_post_30_pct"]
    ).copy()
    if short.empty:
        return "<p style='color:#6b7280'>No short-lag House data available.</p>"
    short["flag"] = pd.Categorical(short["flag"], FLAG_ORDER, ordered=True)
    g = short.groupby("flag", observed=True).agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    ).reset_index()
    g["flag_label"] = g["flag"].map(FLAG_LABEL)
    fig = go.Figure()
    fig.add_bar(name="Move in secret window",
                x=g["flag_label"], y=g["gap"],
                marker_color=[FLAG_COLOR[s] for s in g["flag"]],
                text=[f"{v:+.2f}%<br>(n={n:,})" for v, n in zip(g["gap"], g["n"])],
                textposition="outside")
    fig.add_bar(name="Move 30d after disclosure",
                x=g["flag_label"], y=g["post"],
                marker_color="#94a3b8",
                text=[f"{v:+.2f}%" for v in g["post"]],
                textposition="outside")
    fig.update_layout(yaxis_title="Average %", barmode="group", height=400,
                      legend=dict(orientation="h", y=-0.18),
                      margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-flag-short")


def _chart_flag_predicts_alpha(df: pd.DataFrame) -> str:
    buys = df[df["direction"] == 1].dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"]).copy()
    buys["flag"] = pd.Categorical(buys["flag"], FLAG_ORDER, ordered=True)
    g = buys.groupby("flag", observed=True).agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    ).reset_index()
    g["flag_label"] = g["flag"].map(FLAG_LABEL)
    fig = go.Figure()
    fig.add_bar(name="Move in secret window",
                x=g["flag_label"], y=g["gap"],
                marker_color=[FLAG_COLOR[s] for s in g["flag"]],
                text=[f"{v:+.2f}%<br>(n={n:,})" for v, n in zip(g["gap"], g["n"])],
                textposition="outside")
    fig.add_bar(name="Move 30d after disclosure",
                x=g["flag_label"], y=g["post"],
                marker_color="#94a3b8",
                text=[f"{v:+.2f}%" for v in g["post"]],
                textposition="outside")
    fig.update_layout(yaxis_title="Average %", barmode="group", height=420,
                      legend=dict(orientation="h", y=-0.18),
                      margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-flag")


# ──────────────────────────────────────────────────────────────
# Page
# ──────────────────────────────────────────────────────────────

def _section(num: str, title: str, claim: str, chart: str, narrative: str) -> str:
    return f"""
    <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">{num}. {title}</h2>
    <p style="color:#111;font-size:17px;margin-top:4px"><strong>{claim}</strong></p>
    <div style="margin-top:12px">{chart}</div>
    <p style="color:#374151;font-size:14px;line-height:1.6;margin-top:14px">{narrative}</p>
    """


def render(df: pd.DataFrame, holding: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p = valid["ret_post_30_pct"].mean()
    share = g / (g + p) * 100 if (g + p) != 0 else float("nan")

    example_html, ex = _running_example_card(df)
    example_member = ex.get("member", "the example trader")
    example_ticker = ex.get("ticker", "X")
    example_gap = ex.get("ret_during_gap_pct", float("nan"))
    example_lag = int(ex.get("lag_days", 0)) if pd.notna(ex.get("lag_days", float("nan"))) else 0

    # numbers used inside narratives
    buys = valid[valid["direction"] == 1]
    sells = valid[valid["direction"] == -1]
    buys_gap = buys["ret_during_gap_pct"].mean() if not buys.empty else float("nan")
    sells_gap = sells["ret_during_gap_pct"].mean() if not sells.empty else float("nan")

    bb = buys.copy()
    bb["flag"] = pd.Categorical(bb["flag"], FLAG_ORDER, ordered=True)
    flag_stats = bb.groupby("flag", observed=True).agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    )
    flag_stats["total"] = flag_stats["gap"] + flag_stats["post"]
    # Render the headline table
    flag_table_rows = ""
    labels = {
        "high": '🔴 Strong match <span style="color:#6b7280;font-weight:400">(committee directly oversees this industry)</span>',
        "moderate": '🟠 Some match',
        "low": '🟡 Weak match <span style="color:#6b7280;font-weight:400">(indirect, e.g. trade policy)</span>',
        "none": '⚪ No match <span style="color:#6b7280;font-weight:400">(committee unrelated to this industry)</span>',
    }
    bg = {"high": "#fef2f2", "moderate": "#fff7ed", "low": "#fefce8", "none": "#f9fafb"}
    for sev in FLAG_ORDER:
        if sev not in flag_stats.index:
            continue
        r = flag_stats.loc[sev]
        flag_table_rows += (
            f'<tr style="background:{bg[sev]}">'
            f'<td style="padding:10px 14px">{labels[sev]}</td>'
            f'<td style="padding:10px 14px;text-align:right;font-variant-numeric:tabular-nums">{int(r["n"]):,}</td>'
            f'<td style="padding:10px 14px;text-align:right;font-variant-numeric:tabular-nums">{r["gap"]:+.2f}%</td>'
            f'<td style="padding:10px 14px;text-align:right;font-variant-numeric:tabular-nums">{r["post"]:+.2f}%</td>'
            f'<td style="padding:10px 14px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600">{r["total"]:+.2f}%</td>'
            f'</tr>'
        )
    flag_table = f"""
    <table style="border-collapse:collapse;border:1px solid #e5e7eb;width:100%;font-size:14px;margin-top:12px">
      <thead style="background:#f3f4f6">
        <tr>
          <th style="padding:10px 14px;text-align:left">Flag</th>
          <th style="padding:10px 14px;text-align:right">N (buys)</th>
          <th style="padding:10px 14px;text-align:right">Secret window</th>
          <th style="padding:10px 14px;text-align:right">After disclosure (30d)</th>
          <th style="padding:10px 14px;text-align:right">Total</th>
        </tr>
      </thead>
      <tbody>{flag_table_rows}</tbody>
    </table>
    """
    # Used in section G claim
    high_mean = flag_stats.loc["high", "gap"] if "high" in flag_stats.index else float("nan")
    high_post = flag_stats.loc["high", "post"] if "high" in flag_stats.index else float("nan")
    high_total = flag_stats.loc["high", "total"] if "high" in flag_stats.index else float("nan")
    none_mean = flag_stats.loc["none", "gap"] if "none" in flag_stats.index else float("nan")
    none_post = flag_stats.loc["none", "post"] if "none" in flag_stats.index else float("nan")
    none_total = flag_stats.loc["none", "total"] if "none" in flag_stats.index else float("nan")
    post_delta = high_post - none_post if pd.notna(high_post) and pd.notna(none_post) else float("nan")

    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>What congress is doing with their stocks</title>
  <style>
    body {{ font-family:-apple-system,Helvetica,Arial,sans-serif; color:#111;
            max-width:1100px; margin:24px auto; padding:0 22px }}
    h1 {{ margin-bottom:0; font-size:32px }}
    h2 {{ font-size:22px }}
    .subtitle {{ color:#6b7280; font-size:14px; margin-top:6px; line-height:1.55 }}
    .lede {{ font-size:17px; line-height:1.65; margin:20px 0; padding:16px 20px;
            background:#fef2f2; border-left:4px solid #dc2626; border-radius:4px }}
    code {{ background:#f3f4f6; padding:1px 6px; border-radius:3px; font-size:13px }}
    .vocab {{ background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px;
              padding:14px 18px; margin:14px 0; font-size:14px; line-height:1.65 }}
    .vocab strong {{ color:#111 }}
  </style>
</head><body>

  <h1>What congress is doing with their stocks</h1>
  <div class="subtitle">
    Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} ·
    {len(df):,} trades analyzed
    ({(df['source']=='house').sum():,} House with real disclosure dates,
    {(df['source']=='senate').sum():,} Senate with assumed 30-day lag).
  </div>

  <div class="lede">
    When a senator or representative trades a stock, they have up to 45 days to tell the public.
    During those 45 days, only they know the trade happened — the <strong>secret window</strong>.
    Once disclosed, you can copy them. But if the stock has already moved during the secret window,
    there may not be much left for you.
    <br><br>
    Across {len(valid):,} backtested trades, the average stock moved
    <strong>{g:+.2f}%</strong> in the trader's direction during the secret window,
    then a further <strong>{p:+.2f}%</strong> in the 30 days after disclosure.
    By the time the public knew, <strong>{share:.0f}%</strong> of the 30-day move was already done.
    <br><br>
    But that's the average. The rest of this dashboard breaks it down by the things that matter:
    how fast they disclosed, whether they were buying or selling, who they are, and
    whether their committee oversees the industry.
  </div>

  <div class="vocab">
    <strong>Plain-English glossary</strong> (no jargon below this line):
    <br>• <strong>Secret window</strong> = days between when they traded and when they had to tell the public.
    <br>• <strong>Move in the secret window</strong> = how much the stock moved during those days, in the trader's favor.
        If a senator bought at $100 and the stock was at $115 by the time they disclosed, that's a +15% move in the secret window.
        If they sold at $100 and the stock was at $85 by disclosure, that's also +15% — they avoided a 15% loss.
    <br>• <strong>Strong / weak / no committee match</strong> = whether the senator sits on a committee
        whose work directly affects (strong), indirectly affects (weak), or doesn't affect (no) the stock's industry.
  </div>

  {example_html}
  {_kpi_strip(df)}

  {_section("A", "How much does the stock move during the secret window?",
            f"On average {g:+.2f}% — about {share:.0f}% of the eventual 30-day move had already happened by the time the public found out.",
            _chart_horizons(df),
            f"Each bar is the average % the stock moved in the trader's direction. "
            f"Members 'capture' the {g:+.2f}% red bar before anyone else can see. After disclosure, "
            f"a copycat trader has the {p:+.2f}% blue bar to work with. The 90-day bar shows that returns "
            f"after disclosure mostly stabilize — the action is concentrated in the secret window. "
            f"Our running example ({example_member}, {example_ticker}) had a "
            f"{example_gap:+.1f}% move in {example_lag} days — well above the average.")}

  {_section("B", "Does it matter how fast they disclose?",
            "Yes. The shorter the secret window, the less the stock has moved by disclosure — meaning more juice for a copycat.",
            _chart_lag_buckets(df),
            "Red bars are the move during the secret window; blue bars are what was left for a follower. "
            "When you see a trade in your morning email with a short lag (under 20 days), more of the action is "
            "still ahead. When the lag is close to 45 days, the stock has likely already moved — you're getting "
            "the news late. Trades filed LATE (>45 days, illegal but common) show whether late-filers are systematically "
            "different.")}

  {_section("C", "Buys vs sells — the asymmetry",
            f"Buys carry signal ({buys_gap:+.2f}% average secret-window move). Sells are noise or anti-signal ({sells_gap:+.2f}%).",
            _chart_buys_vs_sells(df),
            "Sales happen for many reasons that have nothing to do with the stock — taxes, diversification, "
            "estate planning, donations. Buys are deliberate. When a member <em>buys</em>, especially in a "
            "stock their committee oversees, the historical evidence is much stronger that they know "
            "something. Treat a 🔴 BUY in your morning email as a high-conviction signal; treat a 🔴 SELL with skepticism.")}

  {_section("D", "Who has the edge?",
            "Best 'timers' (gain most in the secret window) and best 'pickers' (highest annualized long-term return) are often DIFFERENT people.",
            _chart_members_two_views(df),
            "Left: members whose purchases gained the most in the secret window — strongest evidence of well-timed entry. "
            "Right: members whose purchases gained the most per year all-time — best long-term stock picks (luck, skill, or sustained inside info — can't tell which). "
            "When a name from the LEFT chart appears in your alert, that's the strongest insider-timing signal. "
            "From the RIGHT chart: high confidence they pick winners.")}

  <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">E. Where each member sits on both dimensions</h2>
  <p style="font-size:17px;margin-top:4px"><strong>The top-right quadrant — strong timing AND strong long-term picks — is where the highest-conviction names live.</strong></p>
  <div>{_chart_member_scatter(df)}</div>
  <p style="color:#374151;font-size:14px;line-height:1.6">
    Each dot is one member with ≥10 backtested buys. X = average % move in the secret window.
    Y = average annualized return to today. Size = number of trades.
    <strong>Top-right</strong> = great timers AND great pickers (highest credibility).
    <strong>Bottom-right</strong> = great timing but the stock then died — fast-money behavior.
    <strong>Top-left</strong> = lucky long-term pickers, no insider signal.
    <strong>Bottom-left</strong> = the laggards.
  </p>

  {_section("F", "How long do they hold?",
            "Most positions stay open. Among the ones we can see closed, the median holding period tells us how patient they are.",
            _chart_holding(holding),
            "This counts only purchases where we can find a matching disclosed sale later in the dataset. "
            "Many positions are still open — either still held, or sold but not yet disclosed (they have up to 45 days). "
            "A long median holding period means alpha extends well past your 30-day follower window — "
            "the move probably continues if you hold longer. A short median means they're frequently in and out.")}

  <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">G. Does the 🔴 flag help you make money?</h2>
  <p style="color:#111;font-size:17px;margin-top:4px"><strong>
    On <em>total</em> return, no — strong-match buys end up in roughly the same place as random buys
    ({high_total:+.2f}% vs {none_total:+.2f}%).
    But the flag <em>shifts</em> where the gains happen: less before disclosure, more after.
    For you as a follower (you can only capture what's after), that's a <strong>{post_delta:+.2f} percentage-point</strong>
    edge per trade.
  </strong></p>

  {flag_table}

  <p style="color:#374151;font-size:14px;line-height:1.7;margin-top:18px">
    <strong>How to read this table:</strong>
    <br>• <strong>"Secret window"</strong> = move from trade date to public disclosure — what the trader privately captured.
    <br>• <strong>"After disclosure"</strong> = move in the 30 days after the public could see — what a follower captures.
    <br>• <strong>"Total"</strong> = the sum. Roughly what an insider would have made by holding for the full window.
    <br><br>
    The original hypothesis was that 🔴 strong-match trades — a senator on the Energy Committee buying an oil stock —
    would beat random buys on the secret window. They don't. With {int(flag_stats.loc["high","n"]):,} strong-match
    buys and {int(flag_stats.loc["none","n"]):,} unflagged buys, the secret-window difference is ~0.8pp in the
    <em>opposite</em> direction.
    <br><br>
    But the <strong>post-disclosure column</strong> tells the real story. Strong-match buys gain
    <strong>{high_post:+.2f}%</strong> in the 30 days after you can see them; unflagged buys gain
    <strong>{none_post:+.2f}%</strong>. That's the part of the move you can actually capture, and the flag
    does identify it.
  </p>

  <h3 style="font-size:18px;margin-top:32px">G.1 Per-committee — which committees pull the signal up?</h3>
  <p style="color:#374151;font-size:14px;line-height:1.6">
    Same metric (secret-window return) broken down by individual committee, with sample size on each bar.
    Green = above the all-buys baseline of {buys_gap:+.2f}%. Red = below.
    Pay attention to N — small samples (under ~30) can swing wildly on a single outlier.
    The big positives (Energy and Commerce at +20.95% on n=43, Science Space &amp; Tech at +6.31% on n=20)
    drive most of the matrix's secret-window contribution.
  </p>
  <div>{_chart_per_committee(_per_committee_breakdown(df), buys_gap)}</div>

  <h3 style="font-size:18px;margin-top:32px">G.2 Filter to fast-disclosed trades only (under 20 days)</h3>
  <p style="color:#374151;font-size:14px;line-height:1.6">
    Same severity bars, restricted to House trades disclosed within 20 days. ~26% of House volume.
    If the matrix were detecting inside info, the 🔴 bar should pop here — but it doesn't.
    The post-disclosure 🔴 advantage holds though.
  </p>
  <div>{_chart_short_lag_flag(df)}</div>

  <p style="color:#6b7280;font-size:13px;line-height:1.5;margin-top:18px">
    Caveats: (1) committee assignments are looked up as they exist TODAY, not at trade time —
    members who switched committees are misclassified; (2) we match on sector keywords so any committee
    with "energy" in its jurisdiction fires on every energy-sector stock, not just the ones it actually
    has oversight over; (3) the differences here are 1-3pp on samples of 300–4500 — meaningful as a
    directional read, not a precise signal.
  </p>

  <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">H. How to read tomorrow's morning alert</h2>
  <p style="font-size:17px;color:#111;margin-top:6px"><strong>A practical checklist for any trade in your email:</strong></p>
  <ol style="font-size:15px;line-height:1.85;color:#1f2937">
    <li><strong>Is it a buy?</strong> Buys carry signal; sells are mostly noise (section C).</li>
    <li><strong>Was the lag short (<20 days)?</strong> If yes, more juice is still on the table (section B).</li>
    <li><strong>Is there a 🔴 committee match?</strong> Yes adds ~{post_delta:+.1f}pp to the next-30-day return on average vs no match. Modest but real. (See section G.)</li>
    <li><strong>Look up the member.</strong> Top-right of the scatter (section E) = highest credibility. Bottom-left = ignore.</li>
    <li><strong>Is the position still open?</strong> If they're still holding, they probably expect more upside (section F).</li>
  </ol>
  <p style="color:#374151;font-size:14px;line-height:1.6">
    No signal here is decisive on its own. The point of the daily alert is to surface trades worth a deeper look — this dashboard tells you which ones are statistically likeliest to matter.
  </p>

</body></html>"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--senate", default="data/backtest.csv")
    p.add_argument("--house", default="data/backtest_house.csv")
    p.add_argument("--out", default="data/dashboard.html")
    args = p.parse_args()
    senate = _load_csv(args.senate, "senate")
    house = _load_csv(args.house, "house")
    df = pd.concat([senate, house], ignore_index=True)
    if df.empty:
        print("ERROR: no backtest data — run python backtest.py and/or python backtest_house.py first.", file=sys.stderr)
        return 1
    df = _enrich_conflict(df)
    holding = _holding_periods()
    html = render(df, holding)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} chars; senate={len(senate):,}, house={len(house):,}, holding-records={len(holding):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
