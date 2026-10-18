"""Organisation definition: group -> brands -> branches -> pdi_agent_master (agent_name, agent_address) pairs.

A branch is identified exactly as the live agent report identifies it (PortfolioDashboard/python/fetchAgentData.py):
each pair selects pdi_agent_master rows where (agent_name_rm = name OR agent_name_zl = name) AND
(address_rm = address OR address_zl = address); the branch's listings are those whose (agent_name, agent_address)
equals one of those rows' Rightmove pair (property_details) or Zoopla pair (property_details_zoopla).
Sales and lettings departments that the portals list separately are simply two pairs in one branch.

name_prefixes (per brand) and sister_name_prefixes (per organisation) are optional and are used ONLY to label a later
listing as 'sister_brand' (same brand, a branch not in this file, e.g. another Winkworth franchise). They never add
listings to a book.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .params import Params


@dataclass
class Branch:
    brand: str
    name: str
    pairs: list[tuple[str, str]]
    notes: str = ""


@dataclass
class Org:
    org: str
    group: str
    brands: list[str]
    branches: list[Branch]
    params: Params
    book_outcodes: list[str] = field(default_factory=list)
    sister_name_prefixes: list[str] = field(default_factory=list)
    description: str = ""
    source_path: str = ""


def load_org(path: str | Path) -> Org:
    path = Path(path)
    d = json.loads(path.read_text(encoding="utf-8"))
    for k in ("org", "group", "brands"):
        if k not in d:
            raise ValueError(f"{path}: missing '{k}'")
    branches: list[Branch] = []
    brands: list[str] = []
    sister = list(d.get("sister_name_prefixes") or [])
    seen = set()
    for b in d["brands"]:
        brand = b["brand"]
        brands.append(brand)
        sister += list(b.get("name_prefixes") or [])
        for br in b.get("branches") or []:
            pairs = []
            for p in br.get("agent_master") or []:
                n, a = (p.get("agent_name") or "").strip(), (p.get("agent_address") or "").strip()
                if not n or not a:
                    raise ValueError(f"{path}: branch {br.get('branch')} has a pair without agent_name/agent_address")
                pairs.append((n, a))
            if not pairs:
                raise ValueError(f"{path}: branch {br.get('branch')} has no agent_master pairs")
            key = (brand, br["branch"])
            if key in seen:
                raise ValueError(f"{path}: duplicate branch {key}")
            seen.add(key)
            branches.append(Branch(brand=brand, name=br["branch"], pairs=pairs, notes=br.get("notes", "")))
    if not branches:
        raise ValueError(f"{path}: no branches")
    return Org(org=d["org"], group=d["group"], brands=brands, branches=branches,
               params=Params.from_dict(d.get("params")),
               book_outcodes=[o.upper().strip() for o in d.get("book_outcodes") or []],
               sister_name_prefixes=sorted(set(sister)), description=d.get("description", ""),
               source_path=str(path))


def name_has_prefix(name: str | None, prefix: str) -> bool:
    """Case-insensitive prefix that ends on a word boundary ('eXp' does not match 'Express Estate Agency')."""
    n, p = (name or "").lower(), prefix.lower().strip()
    if not p or not n.startswith(p):
        return False
    return len(n) == len(p) or not n[len(p)].isalnum()
