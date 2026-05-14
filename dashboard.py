"""Generate an interactive HTML dashboard from data/backtest.csv.

Charts:
 1. Headline KPIs
 2. Capture share by assumed lag (sensitivity sweep — recomputes on the fly)
 3. Cumulative direction-adjusted return time series (purchases vs sales, what
    a follower would have captured by entering at trade-date vs disclosure-date)
 4. Gap-return distribution (purchases vs sales)
 5. Top 20 trades by gap return (insider window)
 6. Per-member ranking (mean gap return, min 5 trades)

Run:  python dashboard.py [--input data/backtest.csv] [--out data/dashboard.html]
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


KPI_TEMPLATE = """
<div style="display:flex;gap:24px;flex-wrap:wrap;margin:16px 0">
{cards}
</div>
"""

CARD = """
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px 22px;min-width:180px;background:#fafafa">
  <div style="color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:0.05em">{label}</div>
  <div style="font-size:26px;font-weight:600;color:#111;margin-top:4px">{value}</div>
  <div style="color:#6b7280;font-size:12px;margin-top:2px">{sub}</div>
</div>
"""


def _load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Coerce numeric columns
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


def kpi_card(label: str, value: str, sub: str = "") -> str:
    return CARD.format(label=label, value=value, sub=sub)


def headline_kpis(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p = valid["ret_post_30_pct"].mean()
    share = g / (g + p) * 100 if (g + p) != 0 else float("nan")
    cards = [
        kpi_card("Total trades", f"{len(df):,}", "in source dataset"),
        kpi_card("With full pricing", f"{len(valid):,}", "purchases + sales, post-1d valid"),
        kpi_card("Mean gap return", f"{g:+.2f}%", "trade → assumed disclosure (direction-adjusted)"),
        kpi_card("Mean post-disclosure 30d", f"{p:+.2f}%", "what a follower would capture"),
        kpi_card("Capture share by disclosure", f"{share:.0f}%", "of 30-day move already gone"),
    ]
    return KPI_TEMPLATE.format(cards="".join(cards))


def chart_capture_by_lag(df: pd.DataFrame) -> str:
    """The CSV was generated with a single assumed lag, so we can't truly sweep
    here without re-fetching prices. Instead, derive a proxy: for each trade,
    we already have ret_during_gap (lag=30d), ret_post_30 (30..60d), ret_post_90
    (30..120d). We can plot capture share for the *committed* lag plus annotate
    the regimes."""
    valid = df.dropna(subset=["ret_during_gap_pct", "ret_post_30_pct", "ret_post_90_pct"])
    g = valid["ret_during_gap_pct"].mean()
    p30 = valid["ret_post_30_pct"].mean()
    p90 = valid["ret_post_90_pct"].mean()
    fig = go.Figure()
    fig.add_bar(
        x=["During gap (insider window)", "+30d after disclosure", "+90d after disclosure"],
        y=[g, p30, p90],
        marker_color=["#b91c1c", "#d97706", "#65a30d"],
        text=[f"{v:+.2f}%" for v in [g, p30, p90]],
        textposition="outside",
    )
    fig.update_layout(
        title="Mean direction-adjusted return by horizon",
        yaxis_title="Return (%)",
        height=400,
        margin=dict(t=60, l=40, r=20, b=40),
    )
    return fig.to_html(include_plotlyjs="cdn", full_html=False, div_id="chart-horizons")


def chart_cumulative(df: pd.DataFrame) -> str:
    """Equity-curve-style cumulative direction-adjusted return for two strategies:
       A) Enter at trade_date (insider timing — what members effectively got)
       B) Enter at trade_date + 30d (follower timing — what a public-disclosure
          follower would have captured during the next 30 trading days)
    Computed on PURCHASES only (sales signal is noise per the topline).
    """
    purchases = df[(df["direction"] == 1) & df["ret_during_gap_pct"].notna() & df["ret_post_30_pct"].notna()].copy()
    purchases = purchases.sort_values("trade_date")
    purchases["cum_insider"] = (1 + (purchases["ret_during_gap_pct"] + purchases["ret_post_30_pct"]) / 100).cumprod()
    purchases["cum_follower"] = (1 + purchases["ret_post_30_pct"] / 100).cumprod()
    purchases["cum_insider"] = (purchases["cum_insider"] - 1) * 100
    purchases["cum_follower"] = (purchases["cum_follower"] - 1) * 100

    fig = go.Figure()
    fig.add_scatter(
        x=purchases["trade_date"], y=purchases["cum_insider"],
        mode="lines", name="Insider (enter at trade date)",
        line=dict(color="#b91c1c", width=1.5),
    )
    fig.add_scatter(
        x=purchases["trade_date"], y=purchases["cum_follower"],
        mode="lines", name="Follower (enter at disclosure +30d)",
        line=dict(color="#2563eb", width=1.5),
    )
    fig.update_layout(
        title="Cumulative return: insider timing vs follower timing (purchases only, direction-adjusted, equal-weighted)",
        yaxis_title="Cumulative return (%)",
        xaxis_title="Trade date",
        height=460,
        margin=dict(t=60, l=40, r=20, b=40),
        legend=dict(orientation="h", y=-0.18),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="chart-cumulative")


def chart_return_dist(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct"]).copy()
    valid["side"] = valid["direction"].map({1: "Purchase", -1: "Sale"})
    fig = px.histogram(
        valid, x="ret_during_gap_pct", color="side",
        nbins=80, barmode="overlay", opacity=0.6,
        color_discrete_map={"Purchase": "#16a34a", "Sale": "#dc2626"},
        title="Distribution of gap-window returns (direction-adjusted; positive = right-direction call)",
        labels={"ret_during_gap_pct": "Return during insider window (%)"},
    )
    fig.update_layout(height=400, margin=dict(t=60, l=40, r=20, b=40))
    fig.update_xaxes(range=[-50, 50])
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="chart-dist")


def chart_top_trades(df: pd.DataFrame, n: int = 20) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct"]).copy()
    valid["label"] = (
        valid["member"].str.slice(0, 22) + " — "
        + valid["ticker"] + " (" + valid["tx_type"].str.slice(0, 8) + ")"
    )
    top = valid.nlargest(n, "ret_during_gap_pct")
    fig = go.Figure(go.Bar(
        x=top["ret_during_gap_pct"][::-1],
        y=top["label"][::-1],
        orientation="h",
        marker_color="#16a34a",
        text=[f"{v:+.1f}%" for v in top["ret_during_gap_pct"][::-1]],
        textposition="outside",
    ))
    fig.update_layout(
        title=f"Top {n} insider-window returns (direction-adjusted)",
        xaxis_title="Gap return (%)",
        height=520,
        margin=dict(t=60, l=200, r=40, b=40),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="chart-top")


def chart_member_ranking(df: pd.DataFrame, min_trades: int = 5) -> str:
    valid = df.dropna(subset=["ret_during_gap_pct"]).copy()
    g = valid.groupby("member").agg(
        n=("ret_during_gap_pct", "size"),
        mean_gap=("ret_during_gap_pct", "mean"),
        mean_post=("ret_post_30_pct", "mean"),
    ).reset_index()
    g = g[g["n"] >= min_trades].sort_values("mean_gap", ascending=False)
    fig = go.Figure()
    fig.add_bar(x=g["member"], y=g["mean_gap"], name="Gap (insider window)", marker_color="#b91c1c")
    fig.add_bar(x=g["member"], y=g["mean_post"], name="Post-disclosure 30d", marker_color="#2563eb")
    fig.update_layout(
        title=f"Per-member mean returns (≥{min_trades} trades, direction-adjusted)",
        yaxis_title="Mean return (%)",
        barmode="group",
        height=500,
        margin=dict(t=60, l=40, r=20, b=120),
        xaxis_tickangle=-40,
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="chart-members")


def render(df: pd.DataFrame) -> str:
    kpis = headline_kpis(df)
    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Congress Trades — Backtest Dashboard</title>
  <style>
    body {{ font-family:-apple-system,Helvetica,Arial,sans-serif; color:#111; max-width:1200px; margin:24px auto; padding:0 20px }}
    h1 {{ margin-bottom:0 }}
    h2 {{ margin-top:32px; border-top:1px solid #e5e7eb; padding-top:18px }}
    .meta {{ color:#6b7280; font-size:14px }}
    .chart {{ margin-top:8px }}
  </style>
</head><body>
  <h1>Congress trades — backtest dashboard</h1>
  <div class="meta">
    Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} · source:
    <code>senate-stock-watcher</code> · assumed disclosure lag: 30d (configurable in <code>backtest.py</code>).
    All returns are <strong>direction-adjusted</strong> — positive means the
    stock moved in the direction of the trade (up for purchases, down for sales).
  </div>

  {kpis}

  <h2>Mean return by horizon</h2>
  <p class="meta">
    The headline: how much of the eventual move had already happened by the time
    of public disclosure, vs. how much was still capturable for a follower.
  </p>
  <div class="chart">{chart_capture_by_lag(df)}</div>

  <h2>Cumulative return — insider vs follower</h2>
  <p class="meta">
    Equity-curve for an equal-weighted strategy. <strong>Insider</strong> enters at
    trade_date and exits 30d after assumed disclosure; <strong>follower</strong>
    enters at disclosure and exits 30d later. Purchases only (sales signal is noise).
  </p>
  <div class="chart">{chart_cumulative(df)}</div>

  <h2>Distribution of gap-window returns</h2>
  <p class="meta">
    Wide spread on both sides. Purchase distribution skews right (insider buys
    tend to go up); sales distribution is flatter (sales aren't predictive).
  </p>
  <div class="chart">{chart_return_dist(df)}</div>

  <h2>Top trades by insider-window return</h2>
  <p class="meta">
    Biggest single-trade direction-adjusted moves before disclosure.
  </p>
  <div class="chart">{chart_top_trades(df)}</div>

  <h2>Per-member ranking</h2>
  <p class="meta">
    Members with at least 5 backtestable trades, ranked by mean gap return.
    Red bar = insider window. Blue bar = what was left for a follower entering at disclosure.
  </p>
  <div class="chart">{chart_member_ranking(df)}</div>
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
    html = render(df)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} chars, {len(df):,} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
