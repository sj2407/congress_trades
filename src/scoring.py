"""Composite scoring for a single congressional trade.

Combines four sub-scores into a 0-12 composite tier:
  1. committee     — direct matrix in conflicts.py (jurisdictional overlap)
  2. cluster       — distinct members trading same ticker, same direction, +/-30d
  3. size          — disclosed amount-range lower bound
  4. policy        — softer indirect committee->sector links

Tiers: 7+ high, 4-6 moderate, 2-3 weak, 0-1 none. A "floor" rule keeps any
trade with committee==3 (direct strong match) at moderate-or-above, so the
existing strong-jurisdiction signal is never demoted by the composite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, Iterable, List, Set, Tuple

from .conflicts import _normalize, SECTOR_KEYWORDS, detect_conflict
from .types import Trade


CLUSTER_WINDOW_DAYS = 30


@dataclass
class CompositeScore:
    committee: int = 0
    cluster: int = 0
    size: int = 0
    policy: int = 0
    total: int = 0
    tier: str = "none"      # "high" | "moderate" | "weak" | "none"
    reasons: List[str] = field(default_factory=list)
    cluster_members: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Indirect committee -> sector map. Softer than the strict matrix.
# Each match contributes one point; capped at 3.
# ---------------------------------------------------------------------------
INDIRECT_JURISDICTION: Dict[str, Set[str]] = {
    # FinServ AI subcommittee: AI policy bears directly on semis / cloud / software
    "digital assets, financial technology, and artificial intelligence": {"tech", "software", "semiconductor"},
    # Foreign Affairs / Foreign Relations: export controls -> chips / defense-tech
    "foreign affairs": {"semiconductor"},
    "foreign relations": {"semiconductor"},
    # Judiciary — antitrust subcommittee touches platforms / mergers
    "antitrust": {"tech", "internet", "media", "telecom"},
    # Energy & Commerce — Health: medical devices, diagnostics
    "energy and commerce — health": {"healthcare", "pharma", "biotech"},
    # HFSC Capital Markets: securities / exchanges / asset mgmt
    "capital markets": {"asset management", "financial"},
    # HFSC Housing & Insurance: REITs, homebuilders, insurers
    "housing and insurance": {"real estate", "reit", "insurance"},
    # Appropriations — almost everything weakly
    "appropriations": {"_weak_broad_"},
}

WEAK_BROAD = "_weak_broad_"


# ---------------------------------------------------------------------------
# Size scoring — based on the lower bound of the disclosed amount range.
# ---------------------------------------------------------------------------
_AMOUNT_LOWER_BOUNDS: List[Tuple[int, int]] = [
    # (lower_bound_dollars, score)
    (250_000, 3),
    (50_000, 2),
    (15_000, 1),
    (0, 0),
]


def _parse_amount_lower(amount_range: str) -> int:
    """Pull the first dollar number out of '$15,001 - $50,000' style strings."""
    if not amount_range:
        return 0
    m = re.search(r"\$([\d,]+)", amount_range)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def score_size(amount_range: str) -> Tuple[int, str]:
    lo = _parse_amount_lower(amount_range)
    for threshold, score in _AMOUNT_LOWER_BOUNDS:
        if lo >= threshold:
            label = {
                3: f"large position (≥$250k)",
                2: f"sizeable position (≥$50k)",
                1: f"moderate position (≥$15k)",
                0: "",
            }[score]
            return score, label
    return 0, ""


# ---------------------------------------------------------------------------
# Cluster scoring — same ticker, same direction (buy/sell), +/-30 days.
# ---------------------------------------------------------------------------
def _direction(transaction_type: str) -> str:
    t = (transaction_type or "").lower()
    if "urchase" in t or t.startswith("buy"):
        return "buy"
    if "ale" in t:
        return "sell"
    return "other"


def score_cluster(
    trade: Trade,
    all_trades: Iterable[Trade],
    window_days: int = CLUSTER_WINDOW_DAYS,
) -> Tuple[int, List[str], str]:
    """Count distinct members trading `trade.ticker` in same direction within window.

    Returns (score, member_names_including_self, label).
    """
    if not trade.ticker or not trade.transaction_date:
        return 0, [trade.member_name], ""
    ticker = trade.ticker.upper()
    direction = _direction(trade.transaction_type)
    lo = trade.transaction_date - timedelta(days=window_days)
    hi = trade.transaction_date + timedelta(days=window_days)

    members: Set[str] = set()
    for other in all_trades:
        if not other.ticker or other.ticker.upper() != ticker:
            continue
        if not other.transaction_date:
            continue
        if other.transaction_date < lo or other.transaction_date > hi:
            continue
        if _direction(other.transaction_type) != direction:
            continue
        members.add(_normalize(other.member_name))

    n = len(members)
    if n >= 4:
        score = 3
    elif n == 3:
        score = 2
    elif n == 2:
        score = 1
    else:
        score = 0
    label = (
        f"cluster: {n} members {direction}ing {ticker} within ±{window_days}d"
        if score > 0 else ""
    )
    return score, sorted(members), label


# ---------------------------------------------------------------------------
# Policy proximity — softer matrix. Returns 0-3 plus reason lines.
# ---------------------------------------------------------------------------
def score_policy_proximity(
    committees: List[str],
    sector: str,
    industry: str,
) -> Tuple[int, List[str]]:
    if not committees:
        return 0, []
    haystack = f"{_normalize(sector or '')} | {_normalize(industry or '')}"
    # Dedupe by indirect-fragment so parent + subcommittees of the same
    # committee only count once. Keep the shortest match (usually the parent).
    fragment_to_comm: Dict[str, str] = {}
    for comm in committees:
        comm_l = _normalize(comm)
        for frag in INDIRECT_JURISDICTION:
            # Word-boundary guard: avoid matching e.g. "foreign affairs"
            # inside Oversight's "Military and Foreign Affairs" subcommittee.
            # Require the fragment to start at the beginning of the
            # committee name OR immediately after "committee on " or "— ".
            if frag not in comm_l:
                continue
            idx = comm_l.find(frag)
            before = comm_l[:idx].rstrip()
            if before and not (before.endswith("committee on") or before.endswith("—")):
                continue
            cur = fragment_to_comm.get(frag)
            if cur is None or len(comm) < len(cur):
                fragment_to_comm[frag] = comm
    hits: List[str] = []
    weak_broad = 0
    for frag, comm in fragment_to_comm.items():
        jurisdiction = INDIRECT_JURISDICTION[frag]
        if WEAK_BROAD in jurisdiction:
            weak_broad += 1
            continue
        for jkey in jurisdiction:
            matched = False
            for needle in SECTOR_KEYWORDS.get(jkey, {jkey}):
                if needle and needle in haystack:
                    hits.append(f"{comm} → {jkey} (indirect)")
                    matched = True
                    break
            if matched:
                break
    n = len(hits)
    score = min(3, n)
    if score == 0 and weak_broad:
        score = 1
        hits.append("Appropriations (broad fiscal jurisdiction — weak)")
    return score, hits


# ---------------------------------------------------------------------------
# Composite — combine all four sub-scores into a tier.
# ---------------------------------------------------------------------------
_SEVERITY_TO_SCORE = {"high": 3, "moderate": 2, "low": 1, "none": 0}


def _tier_from_total(total: int, committee_score: int) -> str:
    # Floor rule: a direct strong committee match (committee==3) is always
    # at least "moderate", even if no other signal fires.
    base = (
        "high" if total >= 7
        else "moderate" if total >= 4
        else "weak" if total >= 2
        else "none"
    )
    if committee_score == 3 and base in ("weak", "none"):
        return "moderate"
    return base


def score_trade(
    trade: Trade,
    all_trades: Iterable[Trade],
    committees: List[str],
    sector: str,
    industry: str,
) -> CompositeScore:
    cs = CompositeScore()

    # 1. Committee match (reuses existing matrix; map severity -> 0-3).
    severity, committee_reasons = detect_conflict(committees, sector, industry)
    cs.committee = _SEVERITY_TO_SCORE.get(severity, 0)
    cs.reasons.extend(committee_reasons)

    # 2. Cluster.
    cs.cluster, cs.cluster_members, cluster_label = score_cluster(trade, all_trades)
    if cluster_label:
        cs.reasons.append(cluster_label)

    # 3. Size.
    cs.size, size_label = score_size(trade.amount_range)
    if size_label:
        cs.reasons.append(size_label)

    # 4. Policy proximity.
    cs.policy, policy_reasons = score_policy_proximity(committees, sector, industry)
    cs.reasons.extend(policy_reasons)

    cs.total = cs.committee + cs.cluster + cs.size + cs.policy
    cs.tier = _tier_from_total(cs.total, cs.committee)
    return cs
