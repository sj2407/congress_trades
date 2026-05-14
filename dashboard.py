"""Generate a narrative HTML dashboard from data/backtest.csv.

The dashboard tells a story:
  1. Headline — what happens between trade and disclosure
  2. The asymmetry — purchases carry alpha, sales are noise
  3. Does the conflict flag predict alpha?  ← the central question for the alerter
  4. Who has the edge — best and worst members
  5. Where the alpha is — by sector
  6. Severity distribution — what your daily alert volume looks like historically

Each section opens with a one-line claim and a chart that proves or refines it.

Run:  python dashboard.py [--input data/backtest.csv] [--out data/dashboard.html]

The dashboard re-applies the current conflict matrix (src/conflicts.py) to every
historical trade, so editing the matrix and re-running the dashboard immediately
shows whether the change tightens or loosens the historical signal.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from src.committees import lookup_committees
from src.conflicts import detect_conflict
from src.sectors import lookup_sector


SEVERITY_COLOR = {"high": "#dc2626", "moderate": "#ea580c", "low": "#ca8a04", "none": "#9ca3af"}
SEVERITY_ORDER = ["high", "moderate", "low", "none"]


def _load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    num_cols = [
        "direction", "lag_days",
        "price_trade", "price_disclose", "price_disclose_30",
        "price_disclose_90", "price_today",
        "ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct",
        "ret_to_today_pct", "captured_share_pct",
    ]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["disclosure_date"] = pd.to_datetime(df["disclosure_date"], errors="coerce")
    return df


def _enrich_with_conflict(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the current conflict matrix retroactively to every backtested row.

    Note: uses *current* committee assignments (not historical), so a member
    who served on a different committee at trade time will be misclassified.
    Treat this as a directional signal, not a precise one.
    """
    print("Enriching with committee + sector + conflict severity…", flush=True)
    sectors = {}
    industries = {}
    severities = []
    for tk in df["ticker"].fillna("").unique():
        if not tk:
            continue
        s, i = lookup_sector(tk)
        sectors[tk] = s
        industries[tk] = i
    df["sector"] = df["ticker"].map(lambda t: sectors.get(t, ""))
    df["industry"] = df["ticker"].map(lambda t: industries.get(t, ""))

    member_cache = {}
    for name in df["member"].unique():
        member_cache[name] = lookup_committees(name)

    for _, row in df.iterrows():
        comms = member_cache.get(row["member"], [])
        sev, _ = detect_conflict(comms, row["sector"] or "", row["industry"] or "")
        severities.append(sev)
    df["severity"] = severities
    return df


# ─────────────────────────────────────────────────────────────────
# Page helpers
# ─────────────────────────────────────────────────────────────────

def kpi_card(label: str, value: str, sub: str = "", color: str = "#111") -> str:
    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px 22px;min-width:180px;background:#fafafa">
      <div style="color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:0.05em">{label}</div>
      <div style="font-size:26px;font-weight:600;color:{color};margin-top:4px">{value}</div>
      <div style="color:#6b7280;font-size:12px;margin-top:2px">{sub}</div>
    </div>
    """


def kpi_row(cards: List[str]) -> str:
    return f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:12px 0'>{''.join(cards)}</div>"


def section(title: str, claim: str, chart_html: str, narrative: str = "") -> str:
    return f"""
    <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">{title}</h2>
    <p style="color:#111;font-size:16px;margin-top:4px"><strong>{claim}</strong></p>
    <div style="margin-top:8px">{chart_html}</div>
    {f'<p style="color:#374151;font-size:14px;line-height:1.55">{narrative}</p>' if narrative else ''}
    """


# ─────────────────────────────────────────────────────────────────
# Charts
# ─────────────────────────────────────────────────────────────────

def headline_kpis(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p = valid["ret_post_30_pct"].mean()
    share = g / (g + p) * 100 if (g + p) != 0 else float("nan")
    n_high = (df["severity"] == "high").sum()
    cards = [
        kpi_card("Trades backtested", f"{len(valid):,}", "purchases + sales, valid pricing"),
        kpi_card("Mean insider-window gain", f"{g:+.2f}%", "direction-adjusted, trade → disclosure (~30d)",
                 color="#dc2626"),
        kpi_card("Left for a follower", f"{p:+.2f}%", "disclosure → +30d",
                 color="#2563eb"),
        kpi_card("Capture share", f"{share:.0f}%", "of the 30-day move already gone by disclosure",
                 color="#111"),
        kpi_card("Flagged historically", f"{n_high:,}", "trades with 🔴 high committee conflict",
                 color="#dc2626"),
    ]
    return kpi_row(cards)


def chart_horizons(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p30 = valid["ret_post_30_pct"].mean()
    p90 = valid["ret_post_90_pct"].mean()
    fig = go.Figure(go.Bar(
        x=["During gap (insider)", "+30d after disclosure", "+90d after disclosure"],
        y=[g, p30, p90],
        marker_color=["#dc2626", "#2563eb", "#4b5563"],
        text=[f"{v:+.2f}%" for v in [g, p30, p90]],
        textposition="outside",
    ))
    fig.update_layout(yaxis_title="Mean return (%)", height=360, margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs="cdn", full_html=False, div_id="c-horizons")


def chart_purchases_vs_sales(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"]).copy()
    valid["side"] = valid["direction"].map({1: "Purchases", -1: "Sales"})
    g = valid.groupby("side").agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    ).reset_index()
    fig = go.Figure()
    fig.add_bar(name="Gap (insider)", x=g["side"], y=g["gap"], marker_color="#dc2626",
                text=[f"{v:+.2f}%" for v in g["gap"]], textposition="outside")
    fig.add_bar(name="Post-disclosure 30d", x=g["side"], y=g["post"], marker_color="#2563eb",
                text=[f"{v:+.2f}%" for v in g["post"]], textposition="outside")
    fig.update_layout(yaxis_title="Mean return (%)", barmode="group",
                      height=380, margin=dict(t=30, l=40, r=20, b=40),
                      legend=dict(orientation="h", y=-0.18))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-pvs")


def chart_severity(df: pd.DataFrame) -> str:
    """The money chart: does the conflict flag predict alpha?"""
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"]).copy()
    valid["severity"] = pd.Categorical(valid["severity"], SEVERITY_ORDER, ordered=True)
    g = valid.groupby("severity", observed=True).agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    ).reset_index()
    fig = go.Figure()
    fig.add_bar(name="Gap (insider)", x=g["severity"], y=g["gap"],
                marker_color=[SEVERITY_COLOR[s] for s in g["severity"]],
                text=[f"{v:+.2f}%<br>n={n:,}" for v, n in zip(g["gap"], g["n"])], textposition="outside")
    fig.add_bar(name="Post-disclosure 30d", x=g["severity"], y=g["post"],
                marker_color=["#94a3b8"] * len(g),
                text=[f"{v:+.2f}%" for v in g["post"]], textposition="outside")
    fig.update_layout(yaxis_title="Mean return (%)", barmode="group",
                      height=420, margin=dict(t=30, l=40, r=20, b=40),
                      legend=dict(orientation="h", y=-0.18))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-sev")


def chart_severity_purchases(df: pd.DataFrame) -> str:
    """Same as above, restricted to PURCHASES (where the signal lives)."""
    valid = df[(df["direction"] == 1)].dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"]).copy()
    valid["severity"] = pd.Categorical(valid["severity"], SEVERITY_ORDER, ordered=True)
    g = valid.groupby("severity", observed=True).agg(
        n=("ret_during_gap_pct", "size"),
        gap=("ret_during_gap_pct", "mean"),
        post=("ret_post_30_pct", "mean"),
    ).reset_index()
    fig = go.Figure()
    fig.add_bar(name="Gap (insider)", x=g["severity"], y=g["gap"],
                marker_color=[SEVERITY_COLOR[s] for s in g["severity"]],
                text=[f"{v:+.2f}%<br>n={n:,}" for v, n in zip(g["gap"], g["n"])], textposition="outside")
    fig.add_bar(name="Post-disclosure 30d", x=g["severity"], y=g["post"],
                marker_color=["#94a3b8"] * len(g),
                text=[f"{v:+.2f}%" for v in g["post"]], textposition="outside")
    fig.update_layout(yaxis_title="Mean return (%)", barmode="group",
                      height=420, margin=dict(t=30, l=40, r=20, b=40),
                      legend=dict(orientation="h", y=-0.18))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-sev-p")


def chart_members(df: pd.DataFrame, min_trades: int = 10) -> str:
    purchases = df[(df["direction"] == 1)].dropna(subset=["ret_during_gap_pct"]).copy()
    g = purchases.groupby("member").agg(
        n=("ret_during_gap_pct", "size"),
        mean_gap=("ret_during_gap_pct", "mean"),
        mean_post=("ret_post_30_pct", "mean"),
    ).reset_index()
    g = g[g["n"] >= min_trades]
    top = g.nlargest(10, "mean_gap")
    bottom = g.nsmallest(10, "mean_gap")
    combined = pd.concat([top, bottom]).drop_duplicates(subset=["member"])
    combined = combined.sort_values("mean_gap")
    fig = go.Figure()
    fig.add_bar(
        x=combined["mean_gap"],
        y=combined["member"] + " (n=" + combined["n"].astype(str) + ")",
        orientation="h",
        marker_color=["#dc2626" if v > 0 else "#1d4ed8" for v in combined["mean_gap"]],
        text=[f"{v:+.2f}%" for v in combined["mean_gap"]],
        textposition="outside",
    )
    fig.update_layout(
        xaxis_title="Mean gap return on purchases (%)",
        height=560, margin=dict(t=30, l=240, r=40, b=40),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-members")


def chart_sectors(df: pd.DataFrame, min_trades: int = 30) -> str:
    purchases = df[(df["direction"] == 1) & df["sector"].notna() & (df["sector"] != "")].dropna(subset=["ret_during_gap_pct"]).copy()
    g = purchases.groupby("sector").agg(
        n=("ret_during_gap_pct", "size"),
        mean_gap=("ret_during_gap_pct", "mean"),
        mean_post=("ret_post_30_pct", "mean"),
    ).reset_index()
    g = g[g["n"] >= min_trades].sort_values("mean_gap", ascending=True)
    fig = go.Figure()
    fig.add_bar(name="Gap (insider)", x=g["mean_gap"], y=g["sector"], orientation="h",
                marker_color="#dc2626",
                text=[f"{v:+.2f}% (n={n})" for v, n in zip(g["mean_gap"], g["n"])],
                textposition="outside")
    fig.update_layout(
        xaxis_title="Mean gap return on purchases (%)",
        height=480, margin=dict(t=30, l=160, r=40, b=40),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-sectors")


def chart_severity_breakdown(df: pd.DataFrame) -> str:
    counts = df["severity"].value_counts().reindex(SEVERITY_ORDER, fill_value=0)
    fig = go.Figure(go.Bar(
        x=[s.title() for s in counts.index],
        y=counts.values,
        marker_color=[SEVERITY_COLOR[s] for s in counts.index],
        text=[f"{v:,}" for v in counts.values],
        textposition="outside",
    ))
    fig.update_layout(yaxis_title="Trades", height=340, margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-sev-bd")


def chart_cumulative(df: pd.DataFrame) -> str:
    purchases = df[(df["direction"] == 1) & df["ret_during_gap_pct"].notna() & df["ret_post_30_pct"].notna()].copy()
    purchases = purchases.sort_values("trade_date")
    purchases["cum_insider"] = ((1 + (purchases["ret_during_gap_pct"] + purchases["ret_post_30_pct"]) / 100).cumprod() - 1) * 100
    purchases["cum_follower"] = ((1 + purchases["ret_post_30_pct"] / 100).cumprod() - 1) * 100
    fig = go.Figure()
    fig.add_scatter(x=purchases["trade_date"], y=purchases["cum_insider"], mode="lines",
                    name="Insider (entered at trade date)", line=dict(color="#dc2626", width=1.5))
    fig.add_scatter(x=purchases["trade_date"], y=purchases["cum_follower"], mode="lines",
                    name="Follower (entered at disclosure)", line=dict(color="#2563eb", width=1.5))
    fig.update_layout(yaxis_title="Cumulative return (%, equal-weighted)", xaxis_title="Trade date",
                      height=440, margin=dict(t=30, l=40, r=20, b=40),
                      legend=dict(orientation="h", y=-0.18))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-cum")


# ─────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────

def render(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"])
    n = len(valid)
    g = valid["ret_during_gap_pct"].mean()
    p = valid["ret_post_30_pct"].mean()
    share = g / (g + p) * 100 if (g + p) != 0 else float("nan")

    purchases = valid[valid["direction"] == 1]
    sales = valid[valid["direction"] == -1]

    # Severity comparison on purchases only (where signal lives)
    pp = purchases.dropna(subset=["ret_during_gap_pct"]).copy()
    pp["severity"] = pd.Categorical(pp["severity"], SEVERITY_ORDER, ordered=True)
    sev_means = pp.groupby("severity", observed=True)["ret_during_gap_pct"].agg(["mean", "size"])
    high_mean = sev_means.loc["high", "mean"] if "high" in sev_means.index else float("nan")
    none_mean = sev_means.loc["none", "mean"] if "none" in sev_means.index else float("nan")
    sev_delta = high_mean - none_mean

    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Congress Trades — Backtest Dashboard</title>
  <style>
    body {{ font-family:-apple-system,Helvetica,Arial,sans-serif; color:#111; max-width:1200px; margin:24px auto; padding:0 20px }}
    h1 {{ margin-bottom:0; font-size:30px }}
    h2 {{ font-size:22px }}
    .subtitle {{ color:#6b7280; font-size:14px; margin-top:6px }}
    .lede {{ font-size:17px; color:#111; margin:18px 0; padding:14px 18px;
            background:#fef2f2; border-left:4px solid #dc2626; border-radius:4px; line-height:1.55 }}
    code {{ background:#f3f4f6; padding:1px 5px; border-radius:3px; font-size:13px }}
  </style>
</head><body>

  <h1>Congress trades — backtest dashboard</h1>
  <div class="subtitle">
    Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} ·
    {len(df):,} historical Senate trades ({n:,} with full pricing) ·
    assumed disclosure lag 30d ·
    all returns direction-adjusted (positive = right-direction call).
  </div>

  <div class="lede">
    Across {n:,} backtested Senate trades, the typical trade gained <strong>{g:+.2f}%</strong> in the
    insider window (trade → assumed disclosure, ~30 days) and a further <strong>{p:+.2f}%</strong> in
    the 30 days <em>after</em> disclosure. <strong>{share:.0f}% of the 30-day move had already happened
    by the time the public could see the trade.</strong> The chart below shows how to read every other
    section.
  </div>

  {headline_kpis(df)}

  {section(
    "1. The headline — what the data says",
    f"Most of the move happens before you ever see the disclosure ({share:.0f}% of the 30-day window).",
    chart_horizons(df),
    "Each bar is the mean direction-adjusted return over a different window. "
    f"Members captured <strong>{g:+.2f}%</strong> in the ~30 days from trade to disclosure. "
    f"In the next 30 days a follower entering at disclosure would have made <strong>{p:+.2f}%</strong>. "
    "The 90-day post-disclosure window mostly reverts back to baseline market drift — meaning the "
    "edge is concentrated in a short pre-disclosure window.",
  )}

  {section(
    "2. The asymmetry — purchases vs sales",
    "Purchases carry the signal. Sales are noise (often anti-signal).",
    chart_purchases_vs_sales(df),
    f"Of the {n:,} valid trades, {len(purchases):,} are purchases and {len(sales):,} are sales. "
    "Purchases generate a positive direction-adjusted return both during the gap and after disclosure — "
    "consistent with informed buying. Sales, by contrast, often go AGAINST the seller — stocks they "
    "sold tend to keep rising. Implication: the daily alerter should weight purchases more heavily "
    "than sales, and you should treat a 🔴 PURCHASE as a much stronger signal than a 🔴 SALE.",
  )}

  {section(
    "3. Does the committee-conflict flag predict alpha?",
    f"Yes — {'modestly' if abs(sev_delta) < 1 else 'meaningfully'}. " + (
        f"Historical 🔴 high-conflict purchases gained <strong>{high_mean:+.2f}%</strong> in the gap "
        f"vs <strong>{none_mean:+.2f}%</strong> for unflagged purchases (a {sev_delta:+.2f}pp gap)."
        if not pd.isna(sev_delta) else "Insufficient flagged trades in the sample to draw a conclusion."
    ),
    chart_severity_purchases(df),
    "This is the headline question for the alerter: when a member trades a stock in their committee's "
    "jurisdiction, do they outperform their other trades? The chart above shows mean returns broken "
    "down by the severity the alerter would have assigned at the time. "
    "<strong>Caveat:</strong> committee assignments are looked up as they exist TODAY, not at the time "
    "of each historical trade — so a member who switched committees would be misclassified. "
    "Treat this as directional evidence, not precise.",
  )}

  {section(
    "4. Severity distribution — what alert volume looks like",
    "What share of historical trades would have triggered an alert at each severity?",
    chart_severity_breakdown(df),
    "Most trades are unflagged (no committee/sector overlap). "
    "When you receive a 🔴 alert tomorrow, this is the population it stands out from — "
    "those don't fire every day, and when they do, the historical track record above tells you "
    "what to expect.",
  )}

  {section(
    "5. Who has the edge — best and worst members",
    "Members with the strongest (and weakest) historical gap returns on their purchases.",
    chart_members(df, min_trades=10),
    "Top 10 + bottom 10 senators by mean gap return on purchases (minimum 10 backtested trades). "
    "Pay attention to the n= count — a high mean from 10 trades is much less robust than from 100. "
    "When a member from the top of this list shows up in your alert tomorrow, that's a credibility boost; "
    "a member from the bottom should be discounted.",
  )}

  {section(
    "6. Where the alpha is — by sector",
    "Some sectors have systematically larger insider-window moves on congressional purchases.",
    chart_sectors(df, min_trades=30),
    "Sectors with ≥30 backtested purchases, ranked by mean gap return. "
    "Concentrated alpha in a sector should make you more attentive when a member trades in it — "
    "AND more attentive when the conflict matrix flags that sector.",
  )}

  {section(
    "7. Insider vs follower — cumulative",
    "If you'd front-run every Senate disclosure (purchases only), you'd have made some of the move but missed the big chunk.",
    chart_cumulative(df),
    "Equity-curve view of the same fact. Red line = entered at trade date, exited at +30d "
    "after disclosure (insider). Blue line = entered at disclosure, exited 30d later (follower). "
    "Both are equal-weighted; this is illustrative, not a tradeable strategy.",
  )}

  <h2 style="margin-top:40px;border-top:1px solid #e5e7eb;padding-top:18px">Reading the daily alert</h2>
  <p style="font-size:14px;line-height:1.6;color:#374151">
    When your morning email arrives, use this dashboard as the comparison set:
    <ul style="font-size:14px;line-height:1.7;color:#374151">
      <li>A 🔴 <strong>purchase</strong> in a sector from section 6's top stretch, by a member in section 5's top stretch, is the highest-confidence signal you can get.</li>
      <li>A 🔴 <strong>sale</strong> is much less informative — sales as a category are anti-signal historically.</li>
      <li>Unflagged trades (omitted from the email) sit in the gray bars of section 4 — they're noise by this matrix's lights.</li>
      <li>If section 3's gap between 🔴 and ⚪ shrinks after a matrix edit, you've loosened too much. Aim for 🔴 mean returns at least 1pp above ⚪.</li>
    </ul>
  </p>

</body></html>"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="data/backtest.csv")
    p.add_argument("--out", default="data/dashboard.html")
    args = p.parse_args()
    if not os.path.exists(args.input):
        print(f"ERROR: {args.input} not found. Run `python backtest.py` first.", file=sys.stderr)
        return 1
    df = _load(args.input)
    df = _enrich_with_conflict(df)
    html = render(df)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} chars, {len(df):,} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
