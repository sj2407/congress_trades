"""Committee <-> stock-sector conflict matrix.

A trade is flagged as a conflict when the member sits on a committee whose
jurisdiction touches the stock's sector/industry. Severity is a coarse signal,
not a legal judgment.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

# Mapping of committee-name fragments to the set of sector/industry keywords
# they have meaningful jurisdiction over. Keys are matched as substrings (case-
# insensitive) against the full committee name (which already includes parent
# for subcommittees).
COMMITTEE_JURISDICTION: Dict[str, Set[str]] = {
    # Defense / national security
    "armed services": {"aerospace", "defense", "weapon", "military"},
    "intelligence": {"defense", "aerospace", "cyber", "surveillance", "communications"},
    "homeland security": {"defense", "cyber", "security", "border"},
    "veterans": {"healthcare", "pharma", "insurance"},

    # Finance (committees specifically govern these industries — not "broad")
    "financial services": {"bank", "insurance", "asset management", "credit", "real estate", "reit", "financial"},
    "banking, housing, and urban affairs": {"bank", "insurance", "real estate", "reit", "financial"},
    # Note: Senate Finance and House Ways & Means write tax law that touches
    # everything; that's too broad to be a useful signal. We instead map their
    # SUBCOMMITTEES explicitly when relevant (e.g. Finance — Health Care).
    "finance — health care": {"healthcare", "pharma", "biotech", "insurance"},
    "finance — energy, natural resources, and infrastructure": {"energy", "oil", "gas", "utilities"},
    "finance — international trade, customs, and global competitiveness": {"_trade_"},

    # Foreign affairs (export controls, sanctions, foreign military sales)
    "foreign relations": {"defense", "aerospace"},
    "foreign affairs": {"defense", "aerospace"},

    # Tech / commerce / telecom
    "commerce, science, and transportation": {
        "tech", "software", "internet", "media", "telecom", "communications",
        "airlines", "auto", "rail", "ev", "semiconductor",
        "aerospace",  # FAA / NTSB oversight covers Boeing et al.
    },
    "energy and commerce": {
        "energy", "oil", "gas", "utilities", "pharma", "healthcare",
        "biotech", "telecom", "communications", "media", "tech",
    },
    "science, space, and technology": {"tech", "software", "semiconductor", "aerospace", "biotech"},

    # Appropriations — only the agency-specific subcommittees carry signal
    "appropriations — defense": {"defense", "aerospace"},
    "appropriations — energy and water": {"energy", "nuclear", "utilities"},
    "appropriations — agriculture": {"agriculture", "food"},
    "appropriations — financial services": {"bank", "financial"},
    "appropriations — homeland security": {"defense", "cyber", "security"},
    "appropriations — transportation, housing": {"airlines", "rail", "auto", "real estate", "construction"},
    "appropriations — labor, health and human services": {"healthcare", "pharma", "biotech"},
    "appropriations — military construction, veterans": {"defense", "construction"},
    "appropriations — commerce, justice, science": {"tech", "telecom"},
    "appropriations — interior": {"energy", "mining", "oil", "gas"},

    # Energy / environment
    "energy and natural resources": {"energy", "oil", "gas", "utilities", "mining", "nuclear", "renewable"},
    "environment and public works": {"energy", "utilities", "chemicals", "construction", "infrastructure"},
    "natural resources": {"energy", "oil", "gas", "mining", "utilities"},

    # Health
    "health, education, labor, and pensions": {"healthcare", "pharma", "biotech", "education", "insurance"},

    # Agriculture
    "agriculture": {"agriculture", "food", "beverages", "fertilizer"},
    "agriculture, nutrition, and forestry": {"agriculture", "food", "beverages", "fertilizer"},

    # Transportation / infrastructure
    "transportation and infrastructure": {"airlines", "auto", "rail", "construction", "infrastructure", "logistics"},

    # Judiciary (antitrust)
    "judiciary": {"tech", "software", "media", "telecom"},

    # Small business
    "small business": set(),  # too broad to be informative
}

# Sector/industry keywords that should be checked against the ticker's
# yfinance sector + industry strings (both lowercased). Curated to map common
# yfinance industries to our conflict keyword vocabulary.
SECTOR_KEYWORDS: Dict[str, Set[str]] = {
    "aerospace": {"aerospace", "defense"},
    "defense": {"defense", "aerospace"},
    "bank": {"bank"},
    "insurance": {"insurance"},
    "asset management": {"asset management", "capital markets"},
    "real estate": {"real estate"},
    "reit": {"reit"},
    "financial": {"financial"},
    "tech": {"technology", "software", "semiconductor", "internet"},
    "software": {"software", "application", "infrastructure"},
    "internet": {"internet", "interactive"},
    "media": {"media", "entertainment", "broadcasting", "publishing"},
    "telecom": {"telecom", "communications"},
    "communications": {"communications", "telecom"},
    "airlines": {"airline"},
    "auto": {"auto", "automobile", "vehicle"},
    "rail": {"rail"},
    "ev": {"electric vehicle"},
    "semiconductor": {"semiconductor"},
    "energy": {"energy", "oil", "gas", "renewable", "solar", "wind"},
    "oil": {"oil"},
    "gas": {"gas"},
    "utilities": {"utilit"},
    "mining": {"mining", "metals"},
    "nuclear": {"nuclear", "uranium"},
    "renewable": {"renewable", "solar", "wind"},
    "chemicals": {"chemical"},
    "construction": {"construction", "engineering"},
    "infrastructure": {"infrastructure", "engineering"},
    "healthcare": {"healthcare", "health care", "hospital", "medical"},
    "pharma": {"pharma", "drug"},
    "biotech": {"biotech"},
    "agriculture": {"agricultur", "farm"},
    "food": {"food", "packaged"},
    "beverages": {"beverage"},
    "fertilizer": {"fertilizer"},
    "logistics": {"logistic", "shipping", "trucking"},
    "education": {"education"},
    "credit": {"credit"},
    "weapon": {"weapon"},
    "military": {"military"},
    "cyber": {"cyber", "security software"},
    "surveillance": {"surveillance"},
    "border": {"border"},
    "security": {"security"},
}

BROAD_KEY = "_broad_"   # deprecated, kept for compatibility
TRADE_KEY = "_trade_"   # special marker: matches any equity (trade policy affects everything)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def detect_conflict(
    committees: List[str],
    sector: str,
    industry: str,
) -> Tuple[str, List[str]]:
    """Return (severity, reasons).

    severity is one of: "high", "moderate", "low", "none".
    """
    sector_l = _normalize(sector or "")
    industry_l = _normalize(industry or "")
    haystack = f"{sector_l} | {industry_l}"

    if not committees:
        return "none", []

    # Collapse subcommittees under their parent: keep only the shortest match
    # per committee-name-fragment (the full parent name).
    seen_parents: dict[str, str] = {}
    for comm in committees:
        comm_l = _normalize(comm)
        for frag in COMMITTEE_JURISDICTION:
            if frag in comm_l:
                cur = seen_parents.get(frag)
                if cur is None or len(comm) < len(cur):
                    seen_parents[frag] = comm

    direct_hits: List[str] = []
    broad_hits: List[str] = []

    for frag, comm in seen_parents.items():
        jurisdiction = COMMITTEE_JURISDICTION[frag]
        if not jurisdiction:
            continue
        if BROAD_KEY in jurisdiction:
            broad_hits.append(f"{comm} (broad fiscal jurisdiction)")
            continue
        if TRADE_KEY in jurisdiction:
            broad_hits.append(f"{comm} (trade policy)")
            continue
        # First sector-keyword match per committee is enough
        for jkey in jurisdiction:
            matched = False
            for needle in SECTOR_KEYWORDS.get(jkey, {jkey}):
                if needle and needle in haystack:
                    direct_hits.append(f"{comm} → {sector or industry} ({jkey})")
                    matched = True
                    break
            if matched:
                break

    if direct_hits:
        return "high", direct_hits + broad_hits
    if broad_hits:
        return "low", broad_hits
    return "none", []
