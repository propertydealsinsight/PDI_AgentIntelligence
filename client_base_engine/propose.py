"""propose-branches: LIST candidate pdi_agent_master rows for a brand, for a person to confirm.

Nothing is decided here. Franchises (Winkworth) and departments (KFH Sales / Lettings) can only be told apart by
address, so the output shows each agent master row with its Rightmove and Zoopla pairs and how many listings each pair
has (in the given outcodes, if any), plus portal pairs that have listings but no agent master row (those are
invisible to the live report and to this engine). --write-draft writes an org JSON with one branch per agent master
row, for editing.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import source
from .db import ReadOnlyDB


def propose(db: ReadOnlyDB, prefixes: list[str], outcodes: list[str], address_like: str | None) -> dict:
    counts: dict[tuple, dict] = {}
    for prefix in prefixes:
        for portal in ("rightmove", "zoopla"):
            for oc in (outcodes or [None]):
                for r in source.propose_pairs(db, portal, prefix, oc):
                    k = (portal, r["agent_name"], r["agent_address"])
                    c = counts.setdefault(k, {"listings": 0, "lets": 0, "first_seen": None, "last_seen": None})
                    c["listings"] += int(r["listings"] or 0)
                    c["lets"] += int(r["lets"] or 0)
                    for f, fn in (("first_seen", min), ("last_seen", max)):
                        v = r[f]
                        c[f] = v if c[f] is None else (fn(c[f], v) if v else c[f])
    masters: dict[int, dict] = {}
    for prefix in prefixes:
        for r in source.master_rows_for_prefix(db, prefix):
            masters[r["id"]] = r
    rows, covered = [], set()
    for m in masters.values():
        rm = ("rightmove", m["agent_name_rm"], m["address_rm"])
        zl = ("zoopla", m["agent_name_zl"], m["address_zl"])
        crm, czl = counts.get(rm, {}), counts.get(zl, {})
        covered.update([rm, zl])
        text = f"{m['address_rm'] or ''} {m['address_zl'] or ''}".lower()
        if address_like and address_like.lower() not in text:
            continue
        if outcodes and not crm and not czl:
            continue
        rows.append({"agent_master_id": m["id"], "agent_master_name": m["agent_master_name"],
                     "agent_name_rm": m["agent_name_rm"], "address_rm": m["address_rm"],
                     "agent_name_zl": m["agent_name_zl"], "address_zl": m["address_zl"],
                     "matching_algo": m["matching_algo"],
                     "rm_listings": crm.get("listings", 0), "rm_lets": crm.get("lets", 0),
                     "zl_listings": czl.get("listings", 0), "zl_lets": czl.get("lets", 0),
                     "last_seen": str(max([x for x in (crm.get("last_seen"), czl.get("last_seen")) if x], default="") or "")[:10]})
    rows.sort(key=lambda r: -(r["rm_listings"] + r["zl_listings"]))
    uncovered = [{"portal": k[0], "agent_name": k[1], "agent_address": k[2], **{x: str(v)[:10] if x.endswith("seen") else v
                  for x, v in c.items()}}
                 for k, c in counts.items() if k not in covered
                 and (not address_like or address_like.lower() in (k[2] or "").lower())]
    uncovered.sort(key=lambda r: -r["listings"])
    return {"prefixes": prefixes, "outcodes": outcodes, "candidates": rows, "pairs_without_agent_master": uncovered}


def draft_org(result: dict, org: str, group: str, brand: str) -> dict:
    """One branch per agent master row; the pair given is the Rightmove one where present (the job form's choice)."""
    branches, used = [], defaultdict(int)
    for r in result["candidates"]:
        name = (r["agent_name_zl"] or "").split(" - ")[-1].strip() if r["agent_name_zl"] and " - " in r["agent_name_zl"] else \
            (r["address_rm"] or r["address_zl"] or "").split(",")[0].strip()
        used[name] += 1
        if used[name] > 1:
            name = f"{name} ({used[name]})"
        pair = ({"agent_name": r["agent_name_rm"], "agent_address": r["address_rm"]} if r["agent_name_rm"] and r["address_rm"]
                else {"agent_name": r["agent_name_zl"], "agent_address": r["address_zl"]})
        branches.append({"branch": name, "agent_master": [pair],
                         "notes": f"agent_master id {r['agent_master_id']}; RM {r['rm_listings']} / ZL {r['zl_listings']} listings"
                                  f"{' in the outcodes' if result['outcodes'] else ''}; CONFIRM"})
    return {"org": org, "group": group, "description": "DRAFT from propose-branches: confirm every branch before running",
            "book_outcodes": result["outcodes"] or [], "brands": [{"brand": brand, "name_prefixes": result["prefixes"],
                                                                     "branches": branches}]}


def print_result(result: dict) -> None:
    c = result["candidates"]
    print(f"\n{len(c)} agent master rows for {result['prefixes']}" + (f" with listings in {result['outcodes']}" if result["outcodes"] else ""))
    print(f"{'id':>6}  {'RM n':>6} {'ZL n':>6}  {'last':10}  Rightmove pair  ||  Zoopla pair  [algo]")
    for r in c:
        print(f"{r['agent_master_id']:>6}  {r['rm_listings']:>6} {r['zl_listings']:>6}  {r['last_seen']:10}  "
              f"{r['agent_name_rm']} | {r['address_rm']}  ||  {r['agent_name_zl']} | {r['address_zl']}  [{r['matching_algo']}]")
    u = result["pairs_without_agent_master"]
    if u:
        print(f"\n{len(u)} portal pairs with listings but NO agent master row (invisible to the report and the engine):")
        for r in u[:40]:
            print(f"  {r['portal']:9} {r['listings']:>6}  {r['agent_name']} | {r['agent_address']}")


def save(result: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
