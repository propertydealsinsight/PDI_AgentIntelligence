"""Event classification: what has happened to one property in a branch's book since the branch last listed it.

Pure functions, no database: the tests drive them with in-memory listings. The rules and thresholds are the ones in
CONTRACT.md section 3; keep the two in step.

Per property the engine emits at most one STATE event, chosen in this order:
  1. with_you_now                      the branch (or another branch of the organisation) has it live now
  2. relisted_with_competitor_sale/let a competitor listing first published after the branch's last listing is live
                                       now and not yet sold/let agreed
  3. otherwise the most recent of:
       sold                            Land Registry sale (address master or price-paid on a listing) after the
                                       branch's last listing; else the portal's sold record; else a sale listing that
                                       ended sold-STC / under offer
       renewal_due / relet_by_anyone   a later rent listing by someone else was let (let agreed, or ended = presumed
                                       let); renewal_due when a competitor let it 8.5-11.5 months ago
       withdrawn                       the latest sale listing ended without a sale agreed, within 6 months, and no
                                       sale has been recorded since
plus price_cut as an OVERLAY row when the live listing behind event 1 or 2 has been reduced.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Callable

from .model import BookProperty, Listing
from .normalise import months_between
from .params import Params

Relation = Callable[[Listing], str]     # -> 'branch' | 'group' | 'sister_brand' | 'competitor'


@dataclass
class Context:
    now: dt.datetime
    params: Params
    relation: Relation
    median_sale: dict[str, float] = field(default_factory=dict)   # outcode -> branch median asking price
    median_rent: dict[str, float] = field(default_factory=dict)   # outcode -> branch median monthly rent


@dataclass
class LandRegistrySale:
    date: dt.datetime
    price: float | None
    source: str = "address_master"


def _d(x: dt.datetime | None) -> str:
    return x.date().isoformat() if x else ""


EPISODE_DAYS = 92   # one agent's listings of one property this close together are one marketing episode


def _agent_key(name: str | None) -> str:
    return (name or "").lower().split(" - ")[0].strip()


def dedupe_cross_portal(listings: list[Listing], now: dt.datetime | None = None, live_days: int = 180) -> list[Listing]:
    """One agent often lists a property on both portals, or re-adverts it. Listings by the same agent (brand part of
    the name), of the same type, whose first-published dates chain within EPISODE_DAYS are one episode. Each episode
    is represented by its live copy if any, else its Rightmove copy, else the most recently updated one. A sale
    episode is dated from its FIRST advert (when it came to market with that agent); a let episode from its LAST
    advert, because the tenancy starts after the last advert is let (agency_lens also took the latest let listing)."""
    groups: dict[tuple, list[Listing]] = {}
    for l in listings:
        groups.setdefault((_agent_key(l.agent_name), l.listing_type), []).append(l)
    out: list[Listing] = []
    for ls in groups.values():
        ls.sort(key=lambda l: l.first_published or dt.datetime.min)
        episodes: list[list[Listing]] = []
        for l in ls:
            if episodes and l.first_published and episodes[-1][-1].first_published and                     (l.first_published - episodes[-1][-1].first_published).days <= EPISODE_DAYS:
                episodes[-1].append(l)
            else:
                episodes.append([l])
        for ep in episodes:
            best = max(ep, key=lambda l: (bool(now) and l.is_live(now, live_days), l.portal == "rightmove",
                                          l.updated or dt.datetime.min))
            dates = [l.first_published for l in ep if l.first_published]
            when = (min(dates) if best.is_sale else max(dates)) if dates else best.first_published
            out.append(best if when == best.first_published else replace(best, first_published=when))
    return out


def _value(ctx: Context, prop: BookProperty, sale: bool, observed: float | None) -> tuple[float | None, str]:
    if observed:
        return observed, "observed"
    hist = prop.last_price(sale)
    if hist:
        return hist, "property_history"
    med = (ctx.median_sale if sale else ctx.median_rent).get(prop.outcode)
    if med:
        return med, "branch_outcode_median"
    return None, "none"


def _fee(ctx: Context, sale: bool, value: float | None) -> float:
    if not value:
        return 0.0
    p = ctx.params
    return round(value * p.sale_fee_pct / 100.0 if sale else value * 12 * p.let_fee_pct / 100.0, 2)


def _row(ctx: Context, prop: BookProperty, event: str, event_date, listing: Listing | None, *, sale: bool,
         price: float | None, detail: str, confidence: str, relation: str, fee: bool = True,
         gap: float | None = None) -> dict:
    last = prop.last
    value, basis = _value(ctx, prop, sale, price)
    b = prop.branch
    return {
        "org": b.org, "group": b.group, "brand": b.brand, "branch": b.branch,
        "property_key": prop.key, "uprn": prop.uprn or "", "address": prop.address, "postcode": prop.postcode,
        "outcode": prop.outcode, "match_basis": prop.match_basis,
        "branch_last_type": last.listing_type, "branch_last_date": _d(last.first_published),
        "branch_last_price": last.price, "branch_last_status": last.status_text(), "branch_last_portal": last.portal,
        "branch_last_url": last.url,   # the branch's own listing (19 Oct 2026: the page linked the competitor's under "your last listing")
        "event": event, "event_date": _d(event_date), "detail": detail,
        "current_agent": listing.agent_name if listing else "", "current_agent_relation": relation,
        "current_price": price, "price_type": "sale" if sale else "monthly_rent",
        "portal": listing.portal if listing else "", "url": listing.url if listing else "",
        "listing_id": listing.listing_id if listing else "", "date_basis": "event",
        "event_address": listing.address if listing else "", "event_uprn": (listing.uprn or "") if listing else "",
        "gap_months": round(gap, 1) if gap is not None else None,
        "est_fee": _fee(ctx, sale, value) if fee else 0.0, "fee_basis": ("sale" if sale else "let") if fee else "none",
        "value_used": value if fee else None, "value_basis": basis if fee else "none",
        "confidence": confidence,
    }


def _confidence(prop: BookProperty, l: Listing, relation: str) -> str:
    if relation == "branch":
        return "own_listing"
    if prop.uprn and l.uprn and l.uprn == prop.uprn:
        return "uprn"
    return "address"


def _sold_evidence(prop: BookProperty, later: list[Listing], lr: LandRegistrySale | None, since: dt.datetime,
                   floor: dt.datetime) -> tuple[dt.datetime, float | None, str, Listing | None] | None:
    """Most authoritative sale after the branch's last listing: Land Registry, then portal sold record, then a sale
    listing that ended agreed. Returns (date, price, basis, listing that carried it or None)."""
    rows = prop.listings + later
    cands: list[tuple[dt.datetime, float | None, str, Listing | None]] = []
    if lr and lr.date:
        cands.append((lr.date, lr.price, "land_registry", None))
    for l in rows:
        if l.ppd_date:
            cands.append((l.ppd_date, l.ppd_price, "land_registry", l))
    lr_c = [c for c in cands if c[0] > since and c[0] >= floor]
    if lr_c:
        return max(lr_c, key=lambda c: c[0])
    ps = [(l.portal_sold_date, l.portal_sold_price, "portal_sold_record", l) for l in rows if l.portal_sold_date]
    ps = [c for c in ps if c[0] > since and c[0] >= floor]
    if ps:
        return max(ps, key=lambda c: c[0])
    ag = []
    for l in rows:
        if l.is_sale and l.is_agreed() and l.first_published and l.first_published >= since:
            d = l.last_change
            if d and d >= floor:
                ag.append((d, l.price, "portal_agreed", l))
    return max(ag, key=lambda c: c[0]) if ag else None


SELLER_WINDOW_DAYS = 548   # a sale listing more than ~18 months before the completion is not taken as its seller


def _seller_before(prop: BookProperty, later: list[Listing], when: dt.datetime) -> Listing | None:
    lo = when - dt.timedelta(days=SELLER_WINDOW_DAYS)
    sales = [l for l in prop.listings + later
             if l.is_sale and l.first_published and lo <= l.first_published <= when]
    return max(sales, key=lambda l: l.first_published) if sales else None


def classify(prop: BookProperty, later: list[Listing], lr: LandRegistrySale | None, ctx: Context) -> list[dict]:
    """Event rows for one property. `later` = every listing found for the property (any agent, either portal);
    the branch's own listings in it are ignored here because they are already in prop.listings."""
    p, now = ctx.params, ctx.now
    floor = now - dt.timedelta(days=round(p.lookback_months * 30.4))
    last = prop.last
    since = last.first_published or dt.datetime.min
    others = [l for l in later if ctx.relation(l) != "branch" and l.first_published
              and l.first_published > since and l.first_published >= floor]
    others = dedupe_cross_portal(others, now, p.live_days)
    rows: list[dict] = []
    live_listing: Listing | None = None

    own_live = [l for l in prop.listings if l.is_live(now, p.live_days)]
    open_live = [l for l in others if l.is_live(now, p.live_days) and not l.is_agreed()]

    if own_live:
        x = max(own_live, key=lambda l: l.first_published or dt.datetime.min)
        live_listing = x
        also = [l for l in open_live if ctx.relation(l) == "competitor"]
        detail = f"live with the branch since {_d(x.first_published)} ({x.status_text()})"
        if also:
            detail += f"; also listed by {also[0].agent_name}"
        rows.append(_row(ctx, prop, "with_you_now", x.first_published, x, sale=x.is_sale, price=x.price,
                         detail=detail, confidence="own_listing", relation="branch"))
    elif open_live:
        x = max(open_live, key=lambda l: l.first_published)
        live_listing = x
        rel = ctx.relation(x)
        gap = months_between(since, x.first_published)
        if rel == "competitor":
            ev = "relisted_with_competitor_sale" if x.is_sale else "relisted_with_competitor_let"
            cyc = "new cycle" if gap is not None and gap >= p.cycle_gap_months else "same cycle"
            detail = (f"{'for sale' if x.is_sale else 'to let'} with {x.agent_name} since {_d(x.first_published)}, "
                      f"{gap:.0f} months after the branch's last listing ({cyc})")
        else:
            ev = "with_you_now"
            detail = f"live with {x.agent_name} ({rel.replace('_', ' ')}) since {_d(x.first_published)}"
        rows.append(_row(ctx, prop, ev, x.first_published, x, sale=x.is_sale, price=x.price, detail=detail,
                         confidence=_confidence(prop, x, rel), relation=rel, gap=gap))
    else:
        cands: list[tuple[dt.datetime, str, dict]] = []
        s = _sold_evidence(prop, others, lr, since, floor)
        if s:
            sdate, sprice, basis, carrier = s
            seller = carrier if (carrier and basis == "portal_agreed") else _seller_before(prop, others, sdate)
            rel = ctx.relation(seller) if seller else "unknown"
            who = seller.agent_name if seller else "an unknown agent"
            detail = f"sold {_d(sdate)} ({basis.replace('_', ' ')}); last marketed by {who}"
            conf = "uprn" if basis == "land_registry" and carrier is None else (
                _confidence(prop, seller, rel) if seller else "address")
            cands.append((sdate, "sold", _row(ctx, prop, "sold", sdate, seller, sale=True,
                                              price=sprice or (seller.price if seller else None), detail=detail,
                                              confidence=conf, relation=rel)))
        lets = [l for l in others if not l.is_sale and (l.is_agreed() or not l.is_live(now, p.live_days))]
        if lets:
            x = max(lets, key=lambda l: l.first_published)
            rel = ctx.relation(x)
            age = months_between(x.first_published, now)
            gap = months_between(since, x.first_published)
            if rel == "competitor" and age is not None and p.renewal_min_months <= age <= p.renewal_max_months:
                due = x.first_published + dt.timedelta(days=365)
                ev, detail = "renewal_due", f"let by {x.agent_name} ~{age:.0f} months ago; 12-month point ~{_d(due)}"
            else:
                ev, detail = "relet_by_anyone", f"let by {x.agent_name} ({rel.replace('_', ' ')}) ~{age:.0f} months ago"
            cands.append((x.first_published, ev, _row(ctx, prop, ev, x.first_published, x, sale=False, price=x.price,
                                                      detail=detail, confidence=_confidence(prop, x, rel),
                                                      relation=rel, gap=gap)))
        sales = [l for l in ([last] if last.is_sale else []) + [l for l in others if l.is_sale]]
        if sales:
            x = max(sales, key=lambda l: l.first_published or dt.datetime.min)
            ended = x.last_change
            sold_after = s is not None and s[0] >= (x.first_published or dt.datetime.min)
            if (not x.is_live(now, p.live_days) and not x.is_agreed() and not sold_after and ended
                    and months_between(ended, now) is not None and months_between(ended, now) <= p.withdrawn_months):
                rel = ctx.relation(x)
                detail = (f"sale listing with {x.agent_name} (listed {_d(x.first_published)}, last change {_d(ended)}) "
                          f"ended without a sale agreed")
                cands.append((ended, "withdrawn", _row(ctx, prop, "withdrawn", ended, x, sale=True, price=x.price,
                                                       detail=detail, confidence=_confidence(prop, x, rel),
                                                       relation=rel)))
        if cands:
            rows.append(max(cands, key=lambda c: c[0])[2])

    if live_listing is not None:
        cut, pct = live_listing.is_reduced(p.price_cut_min_pct)
        if cut:
            rel = rows[0]["current_agent_relation"]
            when = live_listing.listing_update_date or live_listing.updated
            detail = f"price reduced{f' {pct}%' if pct else ''} on the live listing with {live_listing.agent_name}"
            rows.append(_row(ctx, prop, "price_cut", when, live_listing, sale=live_listing.is_sale,
                             price=live_listing.price, detail=detail, confidence=rows[0]["confidence"],
                             relation=rel, fee=False))
    return rows
