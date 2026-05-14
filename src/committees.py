"""Load current committee assignments from unitedstates/congress-legislators.

Builds a normalized map: member full name -> list of committee names.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Tuple

import requests
import yaml

LEGISLATORS_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml"
COMMITTEES_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committees-current.yaml"
MEMBERSHIP_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committee-membership-current.yaml"


def _fetch_yaml(url: str):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return yaml.safe_load(r.text)


def _normalize_name(s: str) -> str:
    """Lowercase, drop punctuation/suffixes, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|md|phd|esq)\.?\b", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@lru_cache(maxsize=1)
def load_committee_index() -> Tuple[Dict[str, List[str]], Dict[str, Dict]]:
    """Return (member_to_committees, member_info).

    member_to_committees: normalized name -> list of committee names (full names,
      including parent committee where applicable).
    member_info: normalized name -> {full_name, chamber, state, party}.
    """
    legislators = _fetch_yaml(LEGISLATORS_URL)
    committees = _fetch_yaml(COMMITTEES_URL)
    membership = _fetch_yaml(MEMBERSHIP_URL)

    # bioguide_id -> {full_name, chamber, state, party}
    bg_to_info: Dict[str, Dict] = {}
    name_to_bg: Dict[str, str] = {}
    for leg in legislators:
        ids = leg.get("id") or {}
        bg = ids.get("bioguide")
        if not bg:
            continue
        name = leg.get("name") or {}
        full = name.get("official_full") or f"{name.get('first','')} {name.get('last','')}".strip()
        terms = leg.get("terms") or []
        latest = terms[-1] if terms else {}
        chamber = "senate" if latest.get("type") == "sen" else "house"
        info = {
            "full_name": full,
            "chamber": chamber,
            "state": latest.get("state"),
            "party": latest.get("party"),
            "first": name.get("first"),
            "last": name.get("last"),
        }
        bg_to_info[bg] = info
        name_to_bg[_normalize_name(full)] = bg
        # also index "First Last" without middle
        name_to_bg[_normalize_name(f"{name.get('first','')} {name.get('last','')}")] = bg
        # last-name-only fallback (resolved later only if unambiguous)

    # committee_id -> full committee name (with parent)
    comm_id_to_name: Dict[str, str] = {}
    for comm in committees:
        cid = comm.get("thomas_id") or comm.get("senate_committee_id") or comm.get("house_committee_id")
        if cid:
            comm_id_to_name[cid] = comm.get("name", cid)
        for sub in comm.get("subcommittees", []) or []:
            sid = f"{cid}{sub.get('thomas_id','')}" if cid else None
            if sid:
                comm_id_to_name[sid] = f"{comm.get('name','')} — {sub.get('name','')}"

    member_to_committees: Dict[str, List[str]] = {}
    for cid, members in membership.items():
        comm_name = comm_id_to_name.get(cid, cid)
        for m in members or []:
            bg = m.get("bioguide")
            if not bg or bg not in bg_to_info:
                continue
            info = bg_to_info[bg]
            key = _normalize_name(info["full_name"])
            member_to_committees.setdefault(key, []).append(comm_name)

    member_info = {_normalize_name(i["full_name"]): i for i in bg_to_info.values()}
    # also include "first last" keys
    for bg, info in bg_to_info.items():
        alt = _normalize_name(f"{info.get('first','')} {info.get('last','')}")
        if alt not in member_info:
            member_info[alt] = info
            if alt not in member_to_committees and _normalize_name(info["full_name"]) in member_to_committees:
                member_to_committees[alt] = member_to_committees[_normalize_name(info["full_name"])]

    return member_to_committees, member_info


def lookup_committees(member_name: str) -> List[str]:
    m2c, _ = load_committee_index()
    key = _normalize_name(member_name)
    if key in m2c:
        return m2c[key]
    # try last-name "First Last" pattern from a string like "Last, First" or "First M. Last"
    parts = key.split()
    if len(parts) >= 2:
        alt = f"{parts[0]} {parts[-1]}"
        if alt in m2c:
            return m2c[alt]
    return []


def lookup_member_info(member_name: str) -> Dict:
    _, info = load_committee_index()
    return info.get(_normalize_name(member_name), {})
