"""Run an organisation: resolve branches, build each branch's book, find later listings, classify, roll up.

Per branch, sequentially (never concurrent):
  1. pdi_agent_master rows for the branch's pairs -> Rightmove and Zoopla (agent_name, agent_address) pairs
  2. the branch's listings on both portals (any date)                                      -> the book
  3. pdi_address_master for the book's postcodes (UPRN for listings without one; Land Registry last sale)
  4. any agent's listings in those postcodes within the lookback, plus any carrying the book's UPRNs
  5. match later listings to book properties (UPRN first, then the normalised address key) and classify
Postcode, address-master and UPRN reads are cached across branches, so neighbouring branches share them.

Steps 3-5 run in slices of whole outcodes, biggest first (plan_slices). An outcode is never split, so the per-outcode
medians and the address keys (which carry the postcode) are exactly what a single pass gives. After each slice the
optional progress callback gets the cumulative result, so a caller can show the biggest areas within a minute and keep
filling in. A small branch is one slice, unchanged.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections import Counter, defaultdict
from typing import Callable, Optional
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
        self.by_postcode: dict[str, list[Listing]] = defaultdict(list)  # the same listings, by postcode ...
        self.by_uprn: dict[str, list[Listing]] = defaultdict(list)      # ... and by UPRN (slice candidates)
        self.skipped: list[dict] = []

    def add_listing(self, l: Listing) -> None:
        k = (l.portal, l.listing_id)
        if k in self.listings:
            return
        self.listings[k] = l
        if l.postcode:
            self.by_postcode[l.postcode].append(l)
        if l.uprn:
            self.by_uprn[l.uprn].append(l)

    def candidates(self, postcodes: set[str], uprns: set[str]) -> list[Listing]:
        """Later listings read for these postcodes or UPRNs (each once)."""
        out: dict[tuple[str, str], Listing] = {}
        for pc in postcodes:
            for l in self.by_postcode.get(pc, ()):
                out[(l.portal, l.listing_id)] = l
        for u in uprns:
            for l in self.by_uprn.get(u, ()):
                out[(l.portal, l.listing_id)] = l
        return list(out.values())


TWIN_DAYS = 14


def adopt_twin_uprns(listings: list[Listing], days: int = TWIN_DAYS) -> int:
    """A listing without a UPRN takes the UPRN of its twin on the other portal: same postcode, sale/let and price,
    first published within `days` of each other, and exactly one such UPRN. These are all the branch's own listings, so
    the agent name is not compared (it differs by portal: 'Winkworth' / 'Winkworth - Blackheath'). Without this the same home became
    two book properties ('U:<uprn>' from Zoopla, 'A:<address>' from Rightmove when the words are in another order)
    and could carry two contradicting events (19 Oct 2026: 72 The Coach House shown both 'with you now' and
    'withdrawn by you'). Returns how many listings took a UPRN."""
    by: dict[tuple, list[Listing]] = defaultdict(list)
    for l in listings:
        if l.uprn and l.postcode and l.price:
            by[(l.postcode, l.listing_type, round(float(l.price)))].append(l)
    n = 0
    for l in listings:
        if l.uprn or not l.postcode or not l.price or not l.first_published:
            continue
        twins = [t for t in by.get((l.postcode, l.listing_type, round(float(l.price))), ())
                 if t.portal != l.portal and t.first_published and abs((t.first_published - l.first_published).days) <= days]
        uprns = {t.uprn for t in twins}
        if len(uprns) == 1:
            l.uprn = next(iter(uprns))
            n += 1
    return n


SLICE_POSTCODES = 600   # postcodes per slice: about 5 address-master and 10 later-listing reads per portal


def date_takedowns(db: ReadOnlyDB, rows: list[dict], now: dt.datetime, withdrawn_months: int, label: str,
                   skipped: list) -> list[dict]:
    """Withdrawn rows on Rightmove take the real take-down date (status -> archived in the listing history) instead of
    the listing's last change, which is often the day it was listed (data check 19 Oct 2026: 23 of 47 on one branch).
    A row whose take-down is older than the withdrawn window no longer counts and is dropped. Zoopla has no history
    table: its rows keep the last change, marked date_basis 'last_change'."""
    ids = sorted({str(r.get("listing_id")) for r in rows
                  if r.get("event") == "withdrawn" and r.get("portal") == "rightmove" and r.get("listing_id")})
    dates: dict = {}
    if ids:
        try:
            dates, sk = source.takedown_dates(db, ids, label)
            if sk:
                skipped.append({"what": "take-down dates", "branch": label, "keys": len(sk)})
        except Exception:  # noqa: BLE001 - without history the last change stays, labelled as such
            log.warning("take-down dates unavailable for %s", label, exc_info=True)
    out = []
    for r in rows:
        if r.get("event") == "withdrawn":
            d = dates.get(str(r.get("listing_id"))) if r.get("portal") == "rightmove" else None
            if d:
                if (now - d).days > withdrawn_months * 30.5:
                    continue
                r = dict(r, event_date=d.date().isoformat(), date_basis="taken_down")
            else:
                r = dict(r, date_basis="last_change")
        out.append(r)
    return out


def plan_slices(listings: list[Listing], max_postcodes: int = SLICE_POSTCODES) -> list[list[str]]:
    """The branch's outcodes, most listings first, grouped into slices of at most max_postcodes postcodes (an outcode
    with more postcodes than that is a slice on its own). Never splits an outcode."""
    count: Counter = Counter()
    pcs: dict[str, set[str]] = defaultdict(set)
    for l in listings:
        oc = l.outcode or ""
        count[oc] += 1
        if l.postcode:
            pcs[oc].add(l.postcode)
    slices: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for oc in sorted(count, key=lambda o: (-count[o], o)):
        k = len(pcs[oc])
        if cur and size + k > max_postcodes:
            slices.append(cur)
            cur, size = [], 0
        cur.append(oc)
        size += k
    if cur:
        slices.append(cur)
    return slices


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
                c.add_listing(l)
            if skipped:
                c.skipped.append({"what": f"later listings by postcode ({portal})", "branch": label, "keys": len(skipped)})
        todo_u = sorted(u for u in uprns if u not in c.uprn_done[portal])
        if todo_u:
            ls, skipped = source.listings_by_uprn(db, portal, todo_u, lookback, label)
            c.uprn_done[portal].update(todo_u)
            for l in ls:
                c.add_listing(l)
            if skipped:
                c.skipped.append({"what": f"later listings by UPRN ({portal})", "branch": label, "keys": len(skipped)})


def run_org(org: Org, db: ReadOnlyDB, now: dt.datetime | None = None, only_branch: str | None = None,
            progress: Optional[Callable[[dict], None]] = None, slice_postcodes: int = SLICE_POSTCODES) -> dict:
    """Returns {'events': [...], 'book': [...], 'branches': [...], 'skipped': [...]} for the writers and roll-up.

    progress, if given, is called after every slice with the cumulative {'events', 'book', 'branches', 'skipped',
    'progress': {'branch', 'branches_done', 'branches_total', 'slice', 'slices', 'listings_done', 'listings_total',
    'outcodes_done', 'outcodes_total'}}. A callback error is logged and never stops the run."""
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
    todo = [r for r in resolved if r[0] in branches]
    for bi, (b, master_rows, rm, zl, own) in enumerate(todo):
        label = f"{b.brand}/{b.name}"
        t0 = time.time()
        q0 = len(db.timings)
        bid = BranchId(org=org.org, group=org.group, brand=b.brand, branch=b.name)
        listings = (source.branch_listings(db, "rightmove", rm, org.book_outcodes, label)
                    + source.branch_listings(db, "zoopla", zl, org.book_outcodes, label))
        listings = _dedupe_listings(listings)
        if org.book_outcodes:
            listings = [l for l in listings if l.outcode in org.book_outcodes]
        twins = adopt_twin_uprns(listings)
        relation = make_relation(own, org_pairs, org.sister_name_prefixes)
        cutoff12 = now - dt.timedelta(days=365)
        slices = plan_slices(listings, slice_postcodes)
        info = {
            "brand": b.brand, "branch": b.name, "pairs": [list(x) for x in b.pairs],
            "agent_master_rows": [{k: r[k] for k in ("id", "agent_name_rm", "address_rm", "agent_name_zl", "address_zl",
                                                    "matching_algo")} for r in master_rows],
            "rightmove_pairs": sorted([list(x) for x in rm]), "zoopla_pairs": sorted([list(x) for x in zl]),
            "listings": {"rightmove": sum(l.portal == "rightmove" for l in listings),
                         "zoopla": sum(l.portal == "zoopla" for l in listings)},
            "book_properties": 0,
            "book_by_match_basis": {"uprn_on_listing": 0, "uprn_from_address_master": 0, "address_key": 0},
            "properties_with_later_listings": 0,
            "own_listing_uprns": len({l.uprn for l in listings if l.uprn}),
            "book_uprns": 0,
            "competitor_listings_last_12m": {"rightmove": 0, "zoopla": 0, "rightmove_by_listing_uprn": 0, "properties": 0},
            "event_rows": 0, "seconds": 0.0, "queries": 0, "slowest_query_seconds": 0,
            "slices": len(slices), "slices_done": 0, "uprns_from_twin_listing": twins,
        }
        branch_info.append(info)
        listings_done = 0
        outcodes_done = 0
        n_outcodes = sum(len(x) for x in slices)
        for si, ocs in enumerate(slices):
            ocset = set(ocs)
            sl = [l for l in listings if (l.outcode or "") in ocset]
            postcodes = {l.postcode for l in sl if l.postcode}
            _fill_address_master(db, c, postcodes, label)
            book = build_book(bid, sl, c.am_by_key, org.book_outcodes)
            d = diag[b.brand]
            for bp in book:
                d["properties"].add(bp.key)
                rm_strings = {l.address for l in bp.listings if l.portal == "rightmove"}
                d["rightmove_address_strings"].update(rm_strings)
                if not rm_strings:
                    d["zoopla_only_properties"].add(bp.key)
            uprns = {bp.uprn for bp in book if bp.uprn}
            _fill_later(db, c, postcodes, uprns, p.lookback_months, label)
            later = match_later(book, c.candidates(postcodes, uprns), c.am_by_key)
            med_sale, med_rent = _medians(book)
            ctx = Context(now=now, params=p, relation=relation, median_sale=med_sale, median_rent=med_rent)
            comp12 = [l for ls in later.values() for l in ls
                      if l.first_published and l.first_published >= cutoff12 and relation(l) == "competitor"]
            book_keys = {bp.key for bp in book}
            slice_rows: dict = {}
            for bp in book:
                slice_rows[bp.key] = classify(bp, later.get(bp.key, []), c.am_sale.get(bp.uprn) if bp.uprn else None, ctx)
            dated = date_takedowns(db, [r for rs in slice_rows.values() for r in rs], now, p.withdrawn_months, label,
                                   c.skipped)
            by_key: dict = defaultdict(list)
            for r in dated:
                by_key[r["property_key"]].append(r)
            for bp in book:
                rows = by_key.get(bp.key, [])
                events += rows
                info["event_rows"] += len(rows)
                last = bp.last
                book_rows.append({"org": org.org, "group": org.group, "brand": b.brand, "branch": b.name,
                                  "property_key": bp.key, "uprn": bp.uprn or "", "address": bp.address,
                                  "postcode": bp.postcode, "outcode": bp.outcode, "match_basis": bp.match_basis,
                                  "branch_listings": len(bp.listings),
                                  "branch_last_type": last.listing_type,
                                  "branch_last_date": last.first_published.date().isoformat() if last.first_published else "",
                                  "branch_last_price": last.price, "branch_last_status": last.status_text(),
                                  "branch_last_portal": last.portal, "event": rows[0]["event"] if rows else ""})
            info["book_properties"] += len(book)
            for k in info["book_by_match_basis"]:
                info["book_by_match_basis"][k] += sum(bp.match_basis == k for bp in book)
            info["properties_with_later_listings"] += sum(1 for bp in book if later.get(bp.key))
            info["book_uprns"] += len(uprns)
            cc = info["competitor_listings_last_12m"]
            cc["rightmove"] += sum(l.portal == "rightmove" for l in comp12)
            cc["zoopla"] += sum(l.portal == "zoopla" for l in comp12)
            cc["rightmove_by_listing_uprn"] += sum(l.portal == "rightmove" and bool(l.uprn) and f"U:{l.uprn}" in book_keys
                                                   for l in comp12)
            cc["properties"] += len({k for k, ls in later.items() for l in ls if l in comp12})
            qs = db.timings[q0:]
            info["seconds"] = round(time.time() - t0, 1)
            info["queries"] = len(qs)
            info["slowest_query_seconds"] = max((q["seconds"] for q in qs), default=0)
            info["slices_done"] = si + 1
            listings_done += len(sl)
            outcodes_done += len(ocs)
            if progress is not None:
                try:
                    progress({"events": events, "book": book_rows, "branches": branch_info, "skipped": c.skipped,
                              "progress": {"branch": label, "branches_done": bi, "branches_total": len(todo),
                                           "slice": si + 1, "slices": len(slices),
                                           "listings_done": listings_done, "listings_total": len(listings),
                                           "outcodes_done": outcodes_done, "outcodes_total": n_outcodes}})
                except Exception:  # noqa: BLE001 - a display callback never stops the run
                    log.warning("progress callback failed", exc_info=True)
        log.info("%s: %d listings -> %d properties, %d event rows [%ss, %d queries, %d slices, slowest %.1fs]", label,
                 len(listings), info["book_properties"], info["event_rows"], info["seconds"], info["queries"],
                 len(slices), info["slowest_query_seconds"])
    diagnostics = {brand: {k: len(v) for k, v in m.items()} for brand, m in diag.items()}
    return {"events": events, "book": book_rows, "branches": branch_info, "skipped": c.skipped,
            "now": now, "postcodes_read": len(c.am_postcodes), "diagnostics": diagnostics}
