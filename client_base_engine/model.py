"""Plain data objects: one portal listing, one property in a branch's book, and the identity of a branch."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .normalise import address_key, address_postcode, monthly_rent, outcode_of, to_dt

PORTALS = ("rightmove", "zoopla")

# Status vocabulary seen on the two portals (checked on production 18 Oct 2026):
#   Rightmove status: for_sale, to_rent, archived, removed; display_status: '', 'Sold STC', 'Under offer', 'Let agreed'
#   Zoopla status: for_sale, to_rent, sale_under_offer, rented, sold, EXPIRED;
#          display_status: None, 'Just added', 'Back to market', 'Under offer', 'Sold subject to', 'Let'
LIVE_STATUSES = {"for_sale", "to_rent", "sale_under_offer"}
AGREED_SALE_DISPLAY = ("sold stc", "sold subject to", "under offer")
AGREED_SALE_STATUS = {"sold", "sale_under_offer"}
AGREED_LET_STATUS = {"rented"}


@dataclass
class Listing:
    portal: str
    listing_id: str
    agent_name: str
    agent_address: str
    address: str
    postcode: str                       # canonical, from the address text first
    listing_type: str                   # 'sale' | 'let'
    status: str
    display_status: str
    price: float | None                 # asking price (sale) or monthly rent (let)
    listed_price: float | None          # original price where the portal gives one
    first_published: dt.datetime | None
    updated: dt.datetime | None
    listing_update_date: dt.datetime | None = None
    update_reason: str = ""
    uprn: str | None = None
    url: str = ""
    beds: int | None = None
    property_type: str = ""
    ppd_date: dt.datetime | None = None          # HM Land Registry price-paid match on the listing row
    ppd_price: float | None = None
    portal_sold_date: dt.datetime | None = None  # portal's own sold record on the listing row
    portal_sold_price: float | None = None

    @property
    def outcode(self) -> str:
        return outcode_of(self.postcode)

    @property
    def addr_key(self) -> str:
        return address_key(self.address, self.postcode)

    @property
    def is_sale(self) -> bool:
        return self.listing_type == "sale"

    @property
    def pair(self) -> tuple[str, str, str]:
        return (self.portal, self.agent_name or "", self.agent_address or "")

    def is_live(self, now: dt.datetime, live_days: int) -> bool:
        """A for_sale / to_rent listing seen within live_days. Seen = the latest of update_time, the portal's own last
        change and first published: a listing added today has no update_time yet and is live (19 Oct 2026: brand-new
        listings were read as withdrawn)."""
        seen = max((d for d in (self.updated, self.listing_update_date, self.first_published) if d), default=None)
        return (self.status or "") in LIVE_STATUSES and seen is not None and (now - seen).days <= live_days

    def is_agreed(self) -> bool:
        """Sale agreed (STC / under offer / sold) or let agreed, whether or not the listing is still shown."""
        d = (self.display_status or "").strip().lower()
        s = (self.status or "").strip().lower()
        if self.is_sale:
            return s in AGREED_SALE_STATUS or any(x in d for x in AGREED_SALE_DISPLAY)
        return s in AGREED_LET_STATUS or "let agreed" in d or d == "let"

    def is_reduced(self, min_pct: float) -> tuple[bool, float | None]:
        """Price cut: the portal says the last update was a reduction, or the price sits min_pct% under listed_price."""
        pct = None
        if self.listed_price and self.price and self.listed_price > 0:
            pct = round((self.listed_price - self.price) / self.listed_price * 100, 1)
        if pct is not None and pct >= min_pct:
            return True, pct
        if (self.update_reason or "").lower() == "price_reduced":
            return True, pct if pct and pct > 0 else None
        return False, None

    @property
    def last_change(self) -> dt.datetime | None:
        """Last change the portal itself reported (listing_update_date), else first published. update_time is NOT
        used for this: back-office jobs touch it long after a listing has ended (checked 18 Oct 2026)."""
        lud, fp = self.listing_update_date, self.first_published
        if lud and fp:
            return max(lud, fp)
        return lud or fp

    def status_text(self) -> str:
        d = (self.display_status or "").strip()
        return f"{self.status}/{d}" if d else (self.status or "")


def listing_from_row(portal: str, r: dict) -> Listing:
    """Build a Listing from a property_details / property_details_zoopla row (column aliases from source.py)."""
    ls = (r.get("listing_status") or "").lower()
    ltype = "let" if ls == "rent" else "sale"          # 'sale' and 'new-homes' are both sales
    if ltype == "let":
        price = monthly_rent(r.get("price"), r.get("price_per_month"), r.get("price_frequency"))
        listed = monthly_rent(r.get("listed_price"), None, r.get("price_frequency")) if r.get("listed_price") else None
    else:
        price = float(r["price"]) if r.get("price") else None
        listed = float(r["listed_price"]) if r.get("listed_price") else None
    uprn = str(r["uprn"]).strip() if r.get("uprn") not in (None, "") else None
    beds = r.get("num_bedrooms")
    return Listing(
        portal=portal, listing_id=str(r.get("listing_id") or r.get("property_identifier") or ""),
        agent_name=r.get("agent_name") or "", agent_address=r.get("agent_address") or "",
        address=r.get("full_property_address") or "", postcode=address_postcode(r.get("full_property_address"), r.get("postcode")),
        listing_type=ltype, status=r.get("status") or "", display_status=r.get("display_status") or "",
        price=price, listed_price=listed, first_published=to_dt(r.get("first_published_date")),
        updated=to_dt(r.get("update_time")), listing_update_date=to_dt(r.get("listing_update_date")),
        update_reason=r.get("listing_update_reason") or "", uprn=uprn,
        url=r.get("details_url") or r.get("pdi_details_url") or "", beds=int(beds) if beds is not None else None,
        property_type=r.get("property_type") or "",
        ppd_date=to_dt(r.get("pdi_ppd_last_transaction_date")), ppd_price=r.get("pdi_ppd_last_transaction_soldPrice"),
        portal_sold_date=to_dt(r.get("last_transaction_date")), portal_sold_price=r.get("last_transaction_soldPrice"),
    )


@dataclass(frozen=True)
class BranchId:
    org: str
    group: str
    brand: str
    branch: str


@dataclass
class BookProperty:
    """One property in a branch's book: every listing the branch itself has had on it, on either portal."""
    key: str                            # 'U:<uprn>' or 'A:<address key>'
    branch: BranchId
    uprn: str | None
    match_basis: str                    # uprn_on_listing | uprn_from_address_master | address_key
    listings: list[Listing] = field(default_factory=list)

    @property
    def last(self) -> Listing:
        return max(self.listings, key=lambda l: (l.first_published or dt.datetime.min, l.portal == "rightmove"))

    @property
    def address(self) -> str:
        return self.last.address

    @property
    def postcode(self) -> str:
        return self.last.postcode

    @property
    def outcode(self) -> str:
        return self.last.outcode

    def last_price(self, sale: bool) -> float | None:
        ls = [l for l in self.listings if l.is_sale == sale and l.price]
        return max(ls, key=lambda l: l.first_published or dt.datetime.min).price if ls else None
