"""Every database read the engine makes. Read-only, time-capped, indexed predicates only:

  pdi_agent_master          idx_rm (agent_name_rm, address_rm) / idx_zl (agent_name_zl, address_zl)
  property_details(_zoopla) (agent_name, agent_address); (postcode, first_published_date); uprn
  pdi_address_master        postcode

No query scans a table; keys are passed in chunks (db.fetch_chunked) and each chunk is capped below 60s.
"""
from __future__ import annotations

import datetime as dt

from .db import QueryTimeout, ReadOnlyDB, hint
from .model import Listing, listing_from_row

TABLES = {"rightmove": "PDI_PortalsData.property_details", "zoopla": "PDI_PortalsData.property_details_zoopla"}

LISTING_COLS = """property_identifier, listing_id, agent_name, agent_address, full_property_address, postcode, uprn,
    listing_status, status, display_status, price, price_per_month, price_frequency, listed_price,
    first_published_date, update_time, listing_update_date, listing_update_reason, details_url,
    num_bedrooms, property_type, pdi_ppd_last_transaction_date, pdi_ppd_last_transaction_soldPrice,
    last_transaction_date, last_transaction_soldPrice"""

POSTCODE_CHUNK = 120
UPRN_CHUNK = 300


def resolve_pairs(db: ReadOnlyDB, pairs: list[tuple[str, str]], label: str) -> tuple[list[dict], set, set]:
    """agent master rows for a branch, exactly as fetchAgentData.py's T_agent_list; returns (rows, rm_pairs, zl_pairs)."""
    clause = " OR ".join(["((agent_name_rm = %s OR agent_name_zl = %s) AND (address_rm = %s OR address_zl = %s))"] * len(pairs))
    params: list = []
    for n, a in pairs:
        params += [n, n, a, a]
    rows = db.fetch(f"""SELECT {hint(15000)} id, agent_master_name, agent_name_rm, address_rm, agent_name_zl, address_zl,
            matching_algo FROM PDI_PortalsData.pdi_agent_master WHERE {clause} LIMIT 500""", params,
                    label=f"agent_master {label}")
    rm = {(r["agent_name_rm"], r["address_rm"]) for r in rows if r["agent_name_rm"] and r["address_rm"]}
    zl = {(r["agent_name_zl"], r["address_zl"]) for r in rows if r["agent_name_zl"] and r["address_zl"]}
    return rows, rm, zl


def branch_listings(db: ReadOnlyDB, portal: str, pairs: set, outcodes: list[str], label: str) -> list[Listing]:
    """Every listing the branch has had on one portal, any date (same filters as the agent report)."""
    if not pairs:
        return []
    pairs = sorted(pairs)
    clause = " OR ".join(["(agent_name = %s AND agent_address = %s)"] * len(pairs))
    params: list = [v for p in pairs for v in p]
    oc = ""
    if outcodes:
        oc = f" AND outcode IN ({','.join(['%s'] * len(outcodes))})"
        params += outcodes
    sql = f"""SELECT {hint(40000)} {LISTING_COLS} FROM {TABLES[portal]}
        WHERE ({clause}) AND full_property_address IS NOT NULL AND TRIM(postcode) <> ''{oc}"""
    try:
        rows = db.fetch(sql + " LIMIT 100000", params, label=f"book {portal} {label}")
    except QueryTimeout:
        # A national book (eXp: ~72k Rightmove rows) can hit the cap when the database is busy: read it in two-year
        # windows instead of failing the whole build (19 Oct 2026: eXp World UK failed here).
        rows = []
        this_year = dt.date.today().year
        for start in range(2008, this_year + 1, 2):
            rows += db.fetch(sql + f" AND first_published_date >= '{start}-01-01' AND first_published_date < '{start + 2}-01-01'"
                                   " LIMIT 100000", params, label=f"book {portal} {label} {start}-{start + 1}")
        rows += db.fetch(sql + " AND (first_published_date IS NULL OR first_published_date < '2008-01-01') LIMIT 100000",
                         params, label=f"book {portal} {label} undated")
    return [listing_from_row(portal, r) for r in rows]


def address_master(db: ReadOnlyDB, postcodes: list[str], label: str) -> tuple[list[dict], list]:
    def build(ch):
        return (f"""SELECT {hint(20000)} uprn, address, postcode, propertyType, derived_bedrooms, lastSoldDate, lastSoldPrice
            FROM PDI_PortalsData.pdi_address_master WHERE postcode IN ({','.join(['%s'] * len(ch))})""", ch)
    return db.fetch_chunked(build, postcodes, POSTCODE_CHUNK, f"address_master {label}")


def listings_by_postcode(db: ReadOnlyDB, portal: str, postcodes: list[str], lookback_months: int,
                         label: str) -> tuple[list[Listing], list]:
    """Any agent's listings in these postcodes first published within the lookback (postcode+date index)."""
    def build(ch):
        return (f"""SELECT {hint(30000)} {LISTING_COLS} FROM {TABLES[portal]}
            WHERE postcode IN ({','.join(['%s'] * len(ch))})
              AND first_published_date >= DATE_SUB(CURDATE(), INTERVAL {int(lookback_months)} MONTH)
              AND full_property_address IS NOT NULL""", ch)
    rows, skipped = db.fetch_chunked(build, postcodes, POSTCODE_CHUNK, f"later-pc {portal} {label}")
    return [listing_from_row(portal, r) for r in rows], skipped


def listings_by_uprn(db: ReadOnlyDB, portal: str, uprns: list[str], lookback_months: int,
                     label: str) -> tuple[list[Listing], list]:
    """Any agent's listings carrying these UPRNs, first published within the lookback (uprn index)."""
    def build(ch):
        return (f"""SELECT {hint(30000)} {LISTING_COLS} FROM {TABLES[portal]}
            WHERE uprn IN ({','.join(['%s'] * len(ch))})
              AND first_published_date >= DATE_SUB(CURDATE(), INTERVAL {int(lookback_months)} MONTH)""", ch)
    rows, skipped = db.fetch_chunked(build, uprns, UPRN_CHUNK, f"later-uprn {portal} {label}")
    return [listing_from_row(portal, r) for r in rows], skipped


HISTORY_CHUNK = 500


def takedown_dates(db: ReadOnlyDB, listing_ids: list[str], label: str) -> tuple[dict, list]:
    """Rightmove listing id -> the first time its status went to archived (property_details_history, listing_id index).
    The portals do not date a take-down on the listing row itself; this table does, for Rightmove."""
    def build(ch):
        return (f"""SELECT {hint(20000)} listing_id, new_value, changed_at FROM PDI_PortalsData.property_details_history
            WHERE listing_id IN ({','.join(['%s'] * len(ch))}) AND column_name = 'status'""", ch)
    rows, skipped = db.fetch_chunked(build, listing_ids, HISTORY_CHUNK, f"takedown {label}")
    out: dict = {}
    for r in rows:
        if str(r.get("new_value") or "").lower() != "archived" or not r.get("changed_at"):
            continue
        k = str(r["listing_id"])
        if k not in out or r["changed_at"] < out[k]:
            out[k] = r["changed_at"]
    return out, skipped


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def propose_pairs(db: ReadOnlyDB, portal: str, prefix: str, outcode: str | None) -> list[dict]:
    """(agent_name, agent_address) pairs on one portal whose name starts with prefix, with listing counts;
    one outcode at a time when outcodes are given, so each query stays small."""
    oc, params = "", [_like(prefix)]
    if outcode:
        oc, params = " AND outcode = %s", params + [outcode]
    return db.fetch(f"""SELECT {hint(45000)} agent_name, agent_address, COUNT(*) listings,
            SUM(listing_status = 'rent') lets, MIN(first_published_date) first_seen, MAX(first_published_date) last_seen
        FROM {TABLES[portal]} WHERE agent_name LIKE %s{oc}
        GROUP BY agent_name, agent_address LIMIT 5000""", params, label=f"propose {portal} {prefix} {outcode or ''}")


def master_rows_for_prefix(db: ReadOnlyDB, prefix: str) -> list[dict]:
    like = _like(prefix)
    a = db.fetch(f"""SELECT {hint(15000)} id, agent_master_name, agent_name_rm, address_rm, agent_name_zl, address_zl, matching_algo
        FROM PDI_PortalsData.pdi_agent_master WHERE agent_name_rm LIKE %s LIMIT 3000""", [like], label=f"master rm {prefix}")
    b = db.fetch(f"""SELECT {hint(15000)} id, agent_master_name, agent_name_rm, address_rm, agent_name_zl, address_zl, matching_algo
        FROM PDI_PortalsData.pdi_agent_master WHERE agent_name_zl LIKE %s LIMIT 3000""", [like], label=f"master zl {prefix}")
    out = {r["id"]: r for r in a + b}
    return list(out.values())
