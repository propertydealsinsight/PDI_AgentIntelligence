"""Writers for a run: events.csv, events.jsonl, book.csv, summary.json. Column order is the contract (CONTRACT.md)."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .org import Org
from .params import SCHEMA_VERSION
from .rollup import rollup

EVENT_COLUMNS = [
    "org", "group", "brand", "branch",
    "property_key", "uprn", "address", "postcode", "outcode", "match_basis",
    "branch_last_type", "branch_last_date", "branch_last_price", "branch_last_status", "branch_last_portal",
    "event", "event_date", "detail",
    "current_agent", "current_agent_relation", "current_price", "price_type", "portal", "url",
    "gap_months", "est_fee", "fee_basis", "value_used", "value_basis", "confidence",
]

BOOK_COLUMNS = [
    "org", "group", "brand", "branch", "property_key", "uprn", "address", "postcode", "outcode", "match_basis",
    "branch_listings", "branch_last_type", "branch_last_date", "branch_last_price", "branch_last_status",
    "branch_last_portal", "event",
]

DATA_LIMITS = [
    "Portal listings only: asking prices and rents, not achieved ones; PDI cannot see off-portal instructions.",
    "UPRN is filled on a minority of listings overall (about 17% of property_details); the rest match on the "
    "normalised address key (first three words + postcode), which can merge two flats or miss a reformatted address.",
    "Branch identity comes from pdi_agent_master pairs; a branch pair missing from the agent master is invisible, and "
    "known agent-master mis-pairings (review D7) can attach a wrong Zoopla branch.",
    "Let dates are first-published dates; ended rentals are presumed let (some agents never show 'let agreed').",
    "Sold uses Land Registry where the address master or listing carries it (lags completions by weeks to months).",
    "Later listings are read only within the lookback window and only in the book's postcodes or by the book's UPRNs.",
]


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return v


def write_run(out_dir: str | Path, org: Org, result: dict, timings: list[dict], started: dt.datetime) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    events, book = result["events"], result["book"]
    with open(out / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in events:
            w.writerow({k: _fmt(r.get(k)) for k in EVENT_COLUMNS})
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for r in events:
            f.write(json.dumps({k: r.get(k) for k in EVENT_COLUMNS}, ensure_ascii=False, default=str) + "\n")
    with open(out / "book.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BOOK_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in book:
            w.writerow({k: _fmt(r.get(k)) for k in BOOK_COLUMNS})
    summary = {
        "schema_version": SCHEMA_VERSION,
        "org": org.org, "group": org.group, "description": org.description,
        "run_date": result["now"].date().isoformat(), "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "org_file": org.source_path, "book_outcodes": org.book_outcodes,
        "params": org.params.to_dict(),
        "totals": None, "brands": None,
        "run": {"seconds": round((dt.datetime.now() - started).total_seconds(), 1), "queries": len(timings),
                "slowest_query_seconds": max((t["seconds"] for t in timings), default=0),
                "query_errors": [t for t in timings if t.get("error")], "skipped_chunks": result["skipped"],
                "postcodes_read": result["postcodes_read"]},
        "data_limits": DATA_LIMITS,
    }
    ru = rollup(events, book, result["branches"])
    summary["totals"], summary["brands"] = ru["group"], ru["brands"]
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    with open(out / "query_log.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["label", "seconds", "rows", "error"])
        w.writeheader()
        w.writerows(timings)
    return summary
