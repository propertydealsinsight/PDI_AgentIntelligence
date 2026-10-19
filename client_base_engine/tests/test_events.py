"""Event classification with small in-memory fixtures (no database). Run: python -m unittest (from the repo root)."""
from __future__ import annotations

import datetime as dt
import unittest

from client_base_engine.engine import build_book, make_relation, match_later
from client_base_engine.events import Context, LandRegistrySale, classify, dedupe_cross_portal
from client_base_engine.model import BookProperty, BranchId, Listing
from client_base_engine.params import Params
from client_base_engine.rollup import totals

NOW = dt.datetime(2026, 10, 18, 12, 0)
BRANCH = ("rightmove", "Acme", "1 High Street, London, SE3 9LA")
BRANCH_ZL = ("zoopla", "Acme - Blackheath", "1 High Street, Blackheath,")
SISTER_BRANCH = ("rightmove", "Acme", "9 Other Road, London, SE10 8AA")
BID = BranchId(org="acme", group="Acme Group", brand="Acme", branch="Blackheath")


def ago(days: int) -> dt.datetime:
    return NOW - dt.timedelta(days=days)


def months(m: float) -> dt.datetime:
    return NOW - dt.timedelta(days=round(m * 30.4))


def L(who=BRANCH, ltype="sale", status="archived", display="", price=500000.0, fp=None, upd=None, lud=None,
      uprn="100", address="Flat 1, 10 Park Road, London, SE3 9AB", reason="", listed=None, lid=None, **kw) -> Listing:
    portal, name, addr = who
    fp = fp or months(30)
    return Listing(portal=portal, listing_id=lid or f"{name}-{fp.isoformat()}-{ltype}", agent_name=name,
                   agent_address=addr, address=address, postcode="SE3 9AB", listing_type=ltype, status=status,
                   display_status=display, price=price, listed_price=listed, first_published=fp, updated=upd or fp,
                   listing_update_date=lud, update_reason=reason, uprn=uprn, url="https://example/1", **kw)


def competitor(name="Rival & Co", portal="rightmove"):
    return (portal, name, "2 Market Place, London, SE3 0AA")


def ctx(**over) -> Context:
    own = {BRANCH, BRANCH_ZL}
    org_pairs = {BRANCH: ("Acme", "Blackheath"), BRANCH_ZL: ("Acme", "Blackheath"),
                 ("rightmove", "Acme Lettings", "1 High Street, London, SE3 9LA"): ("Acme", "Blackheath Lettings")}
    return Context(now=NOW, params=Params(**over), relation=make_relation(own, org_pairs, ["Acme"]),
                   median_sale={"SE3": 600000.0}, median_rent={"SE3": 2000.0})


def prop(*listings: Listing, uprn="100") -> BookProperty:
    return BookProperty(key=f"U:{uprn}", branch=BID, uprn=uprn, match_basis="uprn_on_listing", listings=list(listings))


def events(rows):
    return [r["event"] for r in rows]


class WithYouNow(unittest.TestCase):
    def test_own_live_listing(self):
        p = prop(L(status="for_sale", fp=ago(40), upd=ago(2)))
        rows = classify(p, [], None, ctx())
        self.assertEqual(events(rows), ["with_you_now"])
        self.assertEqual(rows[0]["confidence"], "own_listing")
        self.assertEqual(rows[0]["est_fee"], 7500.0)                       # 1.5% of 500k

    def test_stale_live_status_is_not_live(self):
        p = prop(L(status="for_sale", fp=ago(400), upd=ago(200)))
        self.assertNotIn("with_you_now", events(classify(p, [], None, ctx())))

    def test_other_org_branch_live_counts_as_with_you(self):
        p = prop(L(fp=months(30)))
        other = L(who=("rightmove", "Acme Lettings", "1 High Street, London, SE3 9LA"), ltype="let", status="to_rent",
                  price=2000.0, fp=ago(20), upd=ago(1))
        rows = classify(p, [other], None, ctx())
        self.assertEqual(events(rows), ["with_you_now"])
        self.assertEqual(rows[0]["current_agent_relation"], "group")

    def test_own_live_wins_over_competitor_live(self):
        p = prop(L(status="for_sale", fp=ago(60), upd=ago(1)))
        rival = L(who=competitor(), status="for_sale", fp=ago(30), upd=ago(1))
        rows = classify(p, [rival], None, ctx())
        self.assertEqual(events(rows), ["with_you_now"])
        self.assertIn("also listed by Rival & Co", rows[0]["detail"])


class Relisted(unittest.TestCase):
    def test_relisted_for_sale_with_competitor(self):
        p = prop(L(fp=months(30), display="Sold STC"))
        rival = L(who=competitor(), status="for_sale", price=640000.0, fp=ago(30), upd=ago(1))
        rows = classify(p, [rival], None, ctx())
        self.assertEqual(events(rows), ["relisted_with_competitor_sale"])
        r = rows[0]
        self.assertEqual((r["current_agent"], r["confidence"], r["est_fee"]), ("Rival & Co", "uprn", 9600.0))
        self.assertGreaterEqual(r["gap_months"], 18)
        self.assertIn("new cycle", r["detail"])

    def test_relisted_to_let_with_competitor(self):
        p = prop(L(ltype="let", price=1800.0, fp=months(14)))
        rival = L(who=competitor(), ltype="let", status="to_rent", price=2100.0, fp=ago(10), upd=ago(1))
        rows = classify(p, [rival], None, ctx())
        self.assertEqual(events(rows), ["relisted_with_competitor_let"])
        self.assertEqual(rows[0]["est_fee"], round(2100 * 12 * 0.12, 2))
        self.assertIn("same cycle", rows[0]["detail"])

    def test_competitor_listing_before_branch_last_is_ignored(self):
        p = prop(L(fp=months(3), status="archived"))
        rival = L(who=competitor(), status="for_sale", fp=months(5), upd=ago(1))
        self.assertNotIn("relisted_with_competitor_sale", events(classify(p, [rival], None, ctx())))

    def test_sister_brand_is_not_a_competitor(self):
        p = prop(L(fp=months(30)))
        sis = L(who=SISTER_BRANCH, status="for_sale", fp=ago(30), upd=ago(1))
        rows = classify(p, [sis], None, ctx())
        self.assertEqual(events(rows), ["with_you_now"])
        self.assertEqual(rows[0]["current_agent_relation"], "sister_brand")

    def test_address_confidence_when_listing_has_no_uprn(self):
        p = prop(L(fp=months(30)))
        rival = L(who=competitor(), status="for_sale", fp=ago(30), upd=ago(1), uprn=None)
        self.assertEqual(classify(p, [rival], None, ctx())[0]["confidence"], "address")


class PriceCut(unittest.TestCase):
    def test_price_cut_overlay_on_competitor_listing(self):
        p = prop(L(fp=months(30)))
        rival = L(who=competitor(), status="for_sale", price=450000.0, listed=500000.0, fp=ago(90), upd=ago(1),
                  lud=ago(10), reason="price_reduced")
        rows = classify(p, [rival], None, ctx())
        self.assertEqual(events(rows), ["relisted_with_competitor_sale", "price_cut"])
        self.assertEqual(rows[1]["est_fee"], 0.0)
        self.assertIn("10.0%", rows[1]["detail"])

    def test_small_drift_is_not_a_cut(self):
        p = prop(L(status="for_sale", price=495000.0, listed=500000.0, fp=ago(40), upd=ago(1)))
        self.assertEqual(events(classify(p, [], None, ctx())), ["with_you_now"])


class SoldWithdrawnLet(unittest.TestCase):
    def test_sold_by_land_registry_after_branch_listing(self):
        own = L(fp=months(14), display="Sold STC", lud=months(13))
        rows = classify(prop(own), [], LandRegistrySale(date=months(10), price=520000), ctx())
        self.assertEqual(events(rows), ["sold"])
        r = rows[0]
        self.assertEqual((r["current_agent_relation"], r["current_price"], r["confidence"]), ("branch", 520000, "uprn"))
        self.assertIn("land registry", r["detail"])

    def test_old_land_registry_sale_outside_lookback_is_ignored(self):
        own = L(fp=months(40))
        rows = classify(prop(own), [], LandRegistrySale(date=months(30), price=1), ctx())
        self.assertEqual(rows, [])

    def test_sold_stc_with_competitor_when_no_land_registry(self):
        p = prop(L(fp=months(30)))
        rival = L(who=competitor(), status="archived", display="Sold STC", fp=months(6), lud=months(4))
        rows = classify(p, [rival], None, ctx())
        self.assertEqual(events(rows), ["sold"])
        self.assertEqual((rows[0]["current_agent_relation"], rows[0]["current_agent"]), ("competitor", "Rival & Co"))
        self.assertIn("portal agreed", rows[0]["detail"])

    def test_seller_more_than_18_months_before_sale_is_unknown(self):
        own = L(ltype="let", price=1500.0, fp=months(23))
        old_sale = L(fp=months(60), lid="old")
        rows = classify(prop(old_sale, own), [], LandRegistrySale(date=months(2), price=400000), ctx())
        self.assertEqual(rows[0]["current_agent_relation"], "unknown")

    def test_withdrawn_own_listing(self):
        own = L(fp=months(7), lud=months(3), status="removed")
        rows = classify(prop(own), [], None, ctx())
        self.assertEqual(events(rows), ["withdrawn"])
        self.assertEqual(rows[0]["current_agent_relation"], "branch")

    def test_withdrawn_uses_portal_last_change_not_update_time(self):
        own = L(fp=months(50), lud=months(49), upd=ago(5), status="removed")   # back-office touched it recently
        self.assertEqual(classify(prop(own), [], None, ctx()), [])

    def test_withdrawn_not_raised_when_sold_since(self):
        own = L(fp=months(7), lud=months(5), status="removed")
        rows = classify(prop(own), [], LandRegistrySale(date=months(1), price=480000), ctx())
        self.assertEqual(events(rows), ["sold"])

    def test_under_offer_ended_is_not_withdrawn(self):
        own = L(fp=months(7), lud=months(3), status="archived", display="Under offer")
        self.assertNotIn("withdrawn", events(classify(prop(own), [], None, ctx())))

    def test_renewal_due_competitor_let_in_window(self):
        p = prop(L(fp=months(30)))
        let = L(who=competitor(), ltype="let", status="archived", display="Let agreed", price=2200.0, fp=months(10))
        rows = classify(p, [let], None, ctx())
        self.assertEqual(events(rows), ["renewal_due"])
        self.assertEqual(rows[0]["est_fee"], round(2200 * 12 * 0.12, 2))

    def test_let_outside_window_is_relet_by_anyone(self):
        p = prop(L(fp=months(30)))
        let = L(who=competitor(), ltype="let", status="EXPIRED", display="Let", price=2200.0, fp=months(5))
        self.assertEqual(events(classify(p, [let], None, ctx())), ["relet_by_anyone"])

    def test_sister_let_in_window_is_relet_not_renewal(self):
        p = prop(L(fp=months(30)))
        let = L(who=SISTER_BRANCH, ltype="let", status="archived", display="Let agreed", price=2200.0, fp=months(10))
        self.assertEqual(events(classify(p, [let], None, ctx())), ["relet_by_anyone"])

    def test_most_recent_outcome_wins(self):
        p = prop(L(fp=months(30)))
        let = L(who=competitor(), ltype="let", status="archived", display="Let agreed", price=2200.0, fp=months(10))
        lr = LandRegistrySale(date=months(2), price=700000)
        self.assertEqual(events(classify(p, [let], lr, ctx())), ["sold"])

    def test_nothing_since_gives_no_row(self):
        self.assertEqual(classify(prop(L(fp=months(30), display="Sold STC")), [], None, ctx()), [])


class FeesAndParams(unittest.TestCase):
    def test_configurable_fees(self):
        p = prop(L(status="for_sale", fp=ago(40), upd=ago(2)))
        self.assertEqual(classify(p, [], None, ctx(sale_fee_pct=1.0))[0]["est_fee"], 5000.0)

    def test_value_falls_back_to_property_history_then_median(self):
        p = prop(L(fp=months(30), price=450000.0))
        rival = L(who=competitor(), status="for_sale", price=None, fp=ago(30), upd=ago(1))
        r = classify(p, [rival], None, ctx())[0]
        self.assertEqual((r["value_used"], r["value_basis"]), (450000.0, "property_history"))
        p2 = prop(L(fp=months(30), ltype="let", price=None))
        rival2 = L(who=competitor(), ltype="let", status="to_rent", price=None, fp=ago(30), upd=ago(1))
        r2 = classify(p2, [rival2], None, ctx())[0]
        self.assertEqual((r2["value_used"], r2["value_basis"]), (2000.0, "branch_outcode_median"))


class BookAndMatching(unittest.TestCase):
    def test_book_keys_uprn_then_address_master_then_address(self):
        a = L(uprn="100", address="Flat 1, 10 Park Road, London, SE3 9AB", lid="a")
        b = L(uprn=None, address="Flat 1, 10 Park Road, London, SE3 9AB", lid="b")        # same address, no UPRN
        c = L(uprn=None, address="22 Hill Lane, London, SE3 9AB", lid="c")                 # address master knows it
        d = L(uprn=None, address="7 Unknown Way, London, SE3 9AB", lid="d")
        book = build_book(BID, [a, b, c, d], {"22 HILL LANE|SE3 9AB": {"555"}}, [])
        by = {p.key: p for p in book}
        self.assertEqual(len(by["U:100"].listings), 2)
        self.assertEqual(by["U:555"].match_basis, "uprn_from_address_master")
        self.assertEqual(by["A:7 UNKNOWN WAY|SE3 9AB"].match_basis, "address_key")

    def test_match_later_uprn_first_then_address(self):
        own = L(uprn="100", address="Flat 1, 10 Park Road, London, SE3 9AB", lid="own")
        book = build_book(BID, [own], {}, [])
        by_uprn = L(who=competitor(), uprn="100", address="Different text, SE3 9AB", lid="x")
        by_addr = L(who=competitor(), uprn=None, address="Flat 1, 10 Park Road, Blackheath, SE3 9AB", lid="y")
        clash = L(who=competitor(), uprn="999", address="Flat 1, 10 Park Road, London, SE3 9AB", lid="z")
        got = match_later(book, [by_uprn, by_addr, clash], {})
        self.assertEqual(sorted(l.listing_id for l in got["U:100"]), ["x", "y"])

    def test_book_outcode_filter(self):
        a = L(lid="a")
        b = L(lid="b", address="5 Far Road, London, N1 1AA")
        b.postcode = "N1 1AA"
        self.assertEqual(len(build_book(BID, [a, b], {}, ["SE3"])), 1)

    def test_cross_portal_duplicate_is_collapsed(self):
        rm = L(who=competitor("Rival"), status="for_sale", fp=ago(30), upd=ago(2), lid="rm")
        zl = L(who=competitor("Rival - Blackheath", "zoopla"), status="for_sale", fp=ago(29), upd=ago(1), lid="zl")
        got = dedupe_cross_portal([rm, zl], NOW, 180)
        self.assertEqual([l.listing_id for l in got], ["rm"])                # Rightmove copy preferred

    def test_let_episode_dated_from_last_advert(self):
        # Rightmove advert 12 months ago, Zoopla copy / re-advert 10 months ago: one tenancy that started after the
        # last advert, so renewal is due (dated 10 months), and the Rightmove copy represents it
        p = prop(L(fp=months(30)))
        rm = L(who=competitor("Rival"), ltype="let", status="archived", display="Let agreed", price=2000.0,
               fp=months(12), lid="rm")
        zl = L(who=competitor("Rival - Lettings", "zoopla"), ltype="let", status="EXPIRED", display="Let", price=2000.0,
               fp=months(10), lid="zl")
        rows = classify(p, [rm, zl], None, ctx())
        self.assertEqual(events(rows), ["renewal_due"])
        self.assertEqual((rows[0]["event_date"], rows[0]["portal"]), (months(10).date().isoformat(), "rightmove"))

    def test_sale_episode_dated_from_first_advert(self):
        p = prop(L(fp=months(30)))
        a = L(who=competitor("Rival"), status="for_sale", fp=ago(80), upd=ago(1), lid="a")
        b = L(who=competitor("Rival", "zoopla"), status="for_sale", fp=ago(20), upd=ago(1), lid="b")
        rows = classify(p, [a, b], None, ctx())
        self.assertEqual(rows[0]["event_date"], ago(80).date().isoformat())

    def test_separate_episodes_stay_separate(self):
        a = L(who=competitor(), ltype="let", fp=months(20), lid="a")
        b = L(who=competitor(), ltype="let", fp=months(9), lid="b")
        self.assertEqual(len(dedupe_cross_portal([a, b], NOW, 180)), 2)


class Rollup(unittest.TestCase):
    def test_brand_level_counts_property_event_once(self):
        r = {"property_key": "U:1", "event": "renewal_due", "est_fee": 3000, "current_agent_relation": "competitor",
             "confidence": "uprn"}
        t = totals([dict(r), dict(r)], {"U:1"}, dedupe=True)
        self.assertEqual((t["events"]["renewal_due"]["count"], t["opportunity_fee"]), (1, 3000))
        t2 = totals([dict(r), dict(r)], {"U:1"}, dedupe=False)
        self.assertEqual(t2["events"]["renewal_due"]["count"], 2)


class NewListingIsLiveTest(unittest.TestCase):
    def test_a_listing_added_today_without_update_time_is_live_not_withdrawn(self):
        new = L(status="for_sale", fp=ago(0), upd=None, lud=ago(0))
        self.assertTrue(new.is_live(NOW, Params().live_days))
        old = L(status="for_sale", fp=ago(400), upd=None, lud=None)
        self.assertFalse(old.is_live(NOW, Params().live_days))


if __name__ == "__main__":
    unittest.main()
