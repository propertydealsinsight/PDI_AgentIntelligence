"""Roll-up: branch -> brand -> group totals by event and by fee, from the same event rows the CSV holds.

Rules (CONTRACT.md section 4):
  * branch totals count every row of that branch;
  * brand and group totals count each (property_key, event) once, so a property in two branches' books (a sales and a
    lettings branch both listed it) is not double counted above branch level. Branch totals can therefore add up to
    more than their brand's;
  * opportunity_fee = est_fee of relisted_with_competitor_sale/let, withdrawn and renewal_due;
    pipeline_fee = est_fee of with_you_now; sold_by_others_fee / sold_by_you_fee split sold by who marketed it;
    price_cut rows carry no fee (the state row for the same property already does).
"""
from __future__ import annotations

from collections import OrderedDict

from .params import EVENTS, OPPORTUNITY_EVENTS


def totals(rows: list[dict], book_keys: set[str], dedupe: bool) -> dict:
    if dedupe:
        seen, kept = set(), []
        for r in rows:
            k = (r["property_key"], r["event"])
            if k not in seen:
                seen.add(k)
                kept.append(r)
        rows = kept
    ev = OrderedDict((e, {"count": 0, "est_fee": 0.0}) for e in EVENTS)
    for r in rows:
        ev[r["event"]]["count"] += 1
        ev[r["event"]]["est_fee"] += float(r["est_fee"] or 0)
    for v in ev.values():
        v["est_fee"] = round(v["est_fee"])
    sold = [r for r in rows if r["event"] == "sold"]
    return {
        "book_properties": len(book_keys),
        "properties_with_event": len({r["property_key"] for r in rows if r["event"] != "price_cut"}),
        "events": ev,
        "opportunity_fee": round(sum(float(r["est_fee"] or 0) for r in rows if r["event"] in OPPORTUNITY_EVENTS)),
        "pipeline_fee": round(sum(float(r["est_fee"] or 0) for r in rows if r["event"] == "with_you_now")),
        "sold_by_you_fee": round(sum(float(r["est_fee"] or 0) for r in sold if r["current_agent_relation"] in ("branch", "group"))),
        "sold_by_others_fee": round(sum(float(r["est_fee"] or 0) for r in sold if r["current_agent_relation"] not in ("branch", "group"))),
        "confidence": {k: sum(1 for r in rows if r["confidence"] == k) for k in ("own_listing", "uprn", "address")},
    }


def rollup(events: list[dict], book: list[dict], branches: list[dict]) -> dict:
    """{'group': totals, 'brands': [{'brand', 'totals', 'branches': [{'branch', 'totals', ...run info}]}]}"""
    brands: "OrderedDict[str, list[dict]]" = OrderedDict()
    for b in branches:
        brands.setdefault(b["brand"], []).append(b)
    out_brands = []
    for brand, bl in brands.items():
        out_branches = []
        for b in bl:
            rows = [r for r in events if r["brand"] == brand and r["branch"] == b["branch"]]
            keys = {r["property_key"] for r in book if r["brand"] == brand and r["branch"] == b["branch"]}
            out_branches.append({"branch": b["branch"], "totals": totals(rows, keys, dedupe=False), "run": b})
        rows = [r for r in events if r["brand"] == brand]
        keys = {r["property_key"] for r in book if r["brand"] == brand}
        out_brands.append({"brand": brand, "totals": totals(rows, keys, dedupe=True), "branches": out_branches})
    return {"group": totals(events, {r["property_key"] for r in book}, dedupe=True), "brands": out_brands}
