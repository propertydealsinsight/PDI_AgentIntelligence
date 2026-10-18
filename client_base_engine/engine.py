"""Run an organisation: resolve branches, build each branch's book, find later listings, classify, roll up.

Per branch, sequentially (never concurrent):
  1. pdi_agent_master rows for the branch's pairs -> Rightmove and Zoopla (agent_name, agent_address) pairs
  2. the branch's listings on both portals (any date)                                      -> the book
  3. pdi_address_master for the book's postcodes (UPRN for listings without one; Land Registry last sale)
  4. any agent's listings in those postcodes within the lookback, plus any carrying the book's UPRNs
  5. match later listings to book properties (UPRN first, then the normalised address key) and classify
Postcode, address-master and UPRN reads are cached across branches, so neighbouring branches share them.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections import defaultdict
from statistics import median

from . import source
from .db import ReadOnlyDB
from .events import Context, LandRegistrySale, classify
from .model import BookProperty, BranchId, Listing
from .normalise import address_key, canon_postcode, outcode_of, to_dt
from .org import Org, name_has_prefix

log = logging.getLogger("client_base_engine.engine")


class Caches:
    def __init__(self):
        self.am_postcodes: set[str] = set()
        self.am_by_key: dict[str, set[str]] = defaultdict(set)       # address key -> UPRNs in the address master
        self.am_sale: dict[str, LandRegistrySale] = {}               # uprn -> last Land Registry sale
        self.pc_done: dict[str, set[str]] = {"rightmove": set(), "zoopla": set()}
        self.uprn_done: dict[str, set[str]] = {"rightmove": set(), "zoopla": set()}
        self.listings: dict[tuple[str, str], Listing] = {}           # (portal, listing_id) -> listing
        self.skipped: list[dict] = []


def _dedupe_listings(ls: list[Listing]) -> list[Listing]:
    out: dict[tuple[str, str], Listing] = {}
    for l in ls:
        out[(l.portal, l.listing_id)] = l
    return list(out.values())


def build_book(branch: BranchId, listings: list[Listing], am_by_key: dict[str, set[str]],
               outcodes: list[str]) -> list[BookProperty]:
    """Group a branch's listings into properties keyed by UPRN, else the normalised address key."""
    if outcodes:
        listings = [l for l in listings if l.outcode in outcodes]
    by_key_uprn: dict[str, set[str]] = defaultdict(set)      # address key -> UPRNs seen on the branch's own listings
    for l in listings:
        if l.uprn:
            by_key_uprn[l.addr_key].add(l.uprn)
    props: dict[str, BookProperty] = {}
    for l in listings:
        ak = l.addr_key
        if l.uprn:
            key, uprn, basis = f"U:{l.uprn}", l.uprn, "uprn_on_listing"
        elif len(by_key_uprn.get(ak, ())) == 1:
            uprn = next(iter(by_key_uprn[ak]))
            key, basis = f"U:{uprn}", "uprn_on_listing"
        elif len(am_by_key.get(ak, ())) == 1:
            uprn = next(iter(am_by_key[ak]))
            key, basis = f"U:{uprn}", "uprn_from_address_master"
        else:
            key, uprn, basis = f"A:{ak}", None, "address_key"
        p = props.get(key)
        if p is None:
            p = props[key] = BookProperty(key=key, branch=branch, uprn=uprn, match_basis=basis)
        elif basis == "uprn_on_listing":
            p.match_basis = basis
        p.listings.append(l)
    return list(props.values())


def match_later(book: list[BookProperty], candidates: list[Listing], am_by_key: dict[str, set[str]]) -> dict[str, list[Listing]]:
    """Later listings per book property: by UPRN first, then by the normalised address key."""
    by_uprn = {p.uprn: p for p in book if p.uprn}
    by_ak: dict[str, BookProperty] = {}
    for p in book:
        for l in p.listings:
            by_ak.setdefault(l.addr_key, p)
    out: dict[str, list[Listing]] = defaultdict(list)
    for l in candidates:
        p = None
        if l.uprn and l.uprn in by_uprn:
            p = by_uprn[l.uprn]
        else:
            ak = l.addr_key
            am = am_by_key.get(ak, set())
            if len(am) == 1 and next(iter(am)) in by_uprn:
                p = by_uprn[next(iter(am))]
            elif ak in by_ak:
                p = by_ak[ak]
                if l.uprn and p.uprn and l.uprn != p.uprn:   # same three words, different property
                    p = None
        if p is not None:
            out[p.key].append(l)
    return out


def _medians(book: list[BookProperty]) -> tuple[dict[str, float], dict[str, float]]:
    sale, rent = defaultdict(list), defaultdict(list)
    for p in book:
        for l in p.listings:
            if l.price:
                (sale if l.is_sale else rent)[l.outcode].append(l.price)
    return ({k: median(v) for k, v in sale.items()}, {k: median(v) for k, v in rent.items()})


def make_relation(own: set, org_pairs: dict, sister_prefixes: list[str]):
    def relation(l: Listing) -> str:
        if l.pair in own:
            return "branch"
        if l.pair in org_pairs:
            return "group"
        if any(name_has_prefix(l.agent_name, p) for p in sister_prefixes):
            return "sister_brand"
        return "competitor"
    return relation


def _fill_address_master(db: ReadOnlyDB, c: Caches, postcodes: set[str], label: str) -> None:
    todo = sorted(pc for pc in postcodes if pc and pc not in c.am_postcodes)
    if not todo:
        return
    rows, skipped = source.address_master(db, todo, label)
    c.am_postcodes.update(todo)
    if skipped:
        c.skipped.append({"what": "address_master", "branch": label, "keys": len(skipped)})
    for r in rows:
        u = str(r["uprn"]).strip() if r.get("uprn") else None
        if not u:
            continue
        c.am_by_key[address_key(r.get("address"), r.get("postcode"))].add(u)
        sd = to_dt(r.get("lastSoldDate"))
        if sd:
            c.am_sale[u] = LandRegistrySale(date=sd, price=r.get("lastSoldPrice"))


def _fill_later(db: ReadOnlyDB, c: Caches, postcodes: set[str], uprns: set[str], lookback: int, label: str) -> None:
    for portal in ("rightmove", "zoopla"):
        todo = sorted(pc for pc in postcodes if pc and pc not in c.pc_done[portal])
        if todo:
            ls, skipped = source.listings_by_postcode(db, portal, todo, lookback, label)
            c.pc_done[portal].update(todo)
            for l in ls:
                c.listings[(l.portal, l.listing_id)] = l
            if skipped:
                c.skipped.append({"what": f"later listings by postcode ({portal})", "branch": label, "keys": len(skipped)})
        todo_u = sorted(u for u in uprns if u not in c.uprn_done[portal])
        if todo_u:
            ls, skipped = source.listings_by_uprn(db, portal, todo_u, lookback, label)
            c.uprn_done[portal].update(todo_u)
            for l in ls:
                c.listings[(l.portal, l.listing_id)] = l
            if skipped:
                c.skipped.append({"what": f"later listings by UPRN ({portal})", "branch": label, "keys": len(skipped)})


def run_org(org: Org, db: ReadOnlyDB, now: dt.datetime | None = None, only_branch: str | None = None) -> dict:
    """Returns {'events': [...], 'book': [...], 'branches': [...], 'skipped': [...]} for the writers and roll-up."""
    now = now or dt.datetime.now()
    p = org.params
    c = Caches()
    branches = [b for b in org.branches if not only_branch or b.name == only_branch]
    if not branches:
        raise ValueError(f"no branch named {only_branch!r}")

    # 1. resolve every branch first: the relation of a later listing (branch / group / competitor) needs all of them
    resolved = []
    org_pairs: dict[tuple, tuple[str, str]] = {}
    for b in org.branches:
        rows, rm, zl = source.resolve_pairs(db, b.pairs, f"{b.brand}/{b.name}")
        own = {("rightmove", n, a) for n, a in rm} | {("zoopla", n, a) for n, a in zl}
        for k in own:
            org_pairs.setdefault(k, (b.brand, b.name))
        resolved.append((b, rows, rm, zl, own))

    events: list[dict] = []
    book_rows: list[dict] = []
    diag: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))   # brand -> measure -> keys
    branch_info: list[dict] = []
    for b, master_rows, rm, zl, own in resolved:
        if b not in branches:
            continue
        label = f"{b.brand}/{b.name}"
        t0 = time.time()
        q0 = len(db.timings)
        bid = BranchId(org=org.org, group=org.group, brand=b.brand, branch=b.name)
        listings = (source.branch_listings(db, "rightmove", rm, org.book_outcodes, label)
                    + source.branch_listings(db, "zoopla", zl, org.book_outcodes, label))
        listings = _dedupe_listings(listings)
        postcodes = {l.postcode for l in listings if l.postcode}
        if org.book_outcodes:
            postcodes = {pc for pc in postcodes if outcode_of(pc) in org.book_outcodes}
        _fill_address_master(db, c, postcodes, label)
        book = build_book(bid, listings, c.am_by_key, org.book_outcodes)
        d = diag[b.brand]
        for bp in book:
            d["properties"].add(bp.key)
            rm_strings = {l.address for l in bp.listings if l.portal == "rightmove"}
            d["rightmove_address_strings"].update(rm_strings)
            if not rm_strings:
                d["zoopla_only_properties"].add(bp.key)
        uprns = {bp.uprn for bp in book if bp.uprn}
        _fill_later(db, c, postcodes, uprns, p.lookback_months, label)
        later = match_later(book, list(c.listings.values()), c.am_by_key)
        med_sale, med_rent = _medians(book)
        relation = make_relation(own, org_pairs, org.sister_name_prefixes)
        ctx = Context(now=now, params=p, relation=relation, median_sale=med_sale, median_rent=med_rent)
        cutoff12 = now - dt.timedelta(days=365)
        comp12 = [l for ls in later.values() for l in ls
                  if l.first_published and l.first_published >= cutoff12 and relation(l) == "competitor"]
        n_ev = 0
        for bp in book:
            rows = classify(bp, later.get(bp.key, []), c.am_sale.get(bp.uprn) if bp.uprn else None, ctx)
            events += rows
            n_ev += len(rows)
            last = bp.last
            book_rows.append({"org": org.org, "group": org.group, "brand": b.brand, "branch": b.name,
                              "property_key": bp.key, "uprn": bp.uprn or "", "address": bp.address,
                              "postcode": bp.postcode, "outcode": bp.outcode, "match_basis": bp.match_basis,
                              "branch_listings": len(bp.listings),
                              "branch_last_type": last.listing_type,
                              "branch_last_date": last.first_published.date().isoformat() if last.first_published else "",
                              "branch_last_price": last.price, "branch_last_status": last.status_text(),
                              "branch_last_portal": last.portal, "event": rows[0]["event"] if rows else ""})
        secs = round(time.time() - t0, 1)
        qs = db.timings[q0:]
        info = {
            "brand": b.brand, "branch": b.name, "pairs": [list(x) for x in b.pairs],
            "agent_master_rows": [{k: r[k] for k in ("id", "agent_name_rm", "address_rm", "agent_name_zl", "address_zl",
                                                    "matching_algo")} for r in master_rows],
            "rightmove_pairs": sorted([list(x) for x in rm]), "zoopla_pairs": sorted([list(x) for x in zl]),
            "listings": {"rightmove": sum(l.portal == "rightmove" for l in listings),
                         "zoopla": sum(l.portal == "zoopla" for l in listings)},
            "book_properties": len(book),
            "book_by_match_basis": {k: sum(bp.match_basis == k for bp in book)
                                    for k in ("uprn_on_listing", "uprn_from_address_master", "address_key")},
            "properties_with_later_listings": sum(1 for bp in book if later.get(bp.key)),
            "own_listing_uprns": len({l.uprn for l in listings if l.uprn}),
            "book_uprns": len(uprns),
            "competitor_listings_last_12m": {
                "rightmove": sum(l.portal == "rightmove" for l in comp12),
                "zoopla": sum(l.portal == "zoopla" for l in comp12),
                "rightmove_by_listing_uprn": sum(l.portal == "rightmove" and bool(l.uprn) and f"U:{l.uprn}" in {bp.key for bp in book}
                                                 for l in comp12),
                "properties": len({k for k, ls in later.items() for l in ls if l in comp12})},
            "event_rows": n_ev, "seconds": secs, "queries": len(qs),
            "slowest_query_seconds": max((q["seconds"] for q in qs), default=0),
        }
        branch_info.append(info)
        log.info("%s: %d listings -> %d properties, %d event rows [%ss, %d queries, slowest %.1fs]", label,
                 len(listings), len(book), n_ev, secs, len(qs), info["slowest_query_seconds"])
    diagnostics = {brand: {k: len(v) for k, v in m.items()} for brand, m in diag.items()}
    return {"events": events, "book": book_rows, "branches": branch_info, "skipped": c.skipped,
            "now": now, "postcodes_read": len(c.am_postcodes), "diagnostics": diagnostics}
