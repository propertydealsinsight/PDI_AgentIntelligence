"""Progressive runs (run_org in outcode slices): the sliced result equals the one-pass result, and progress is reported.
No database: the source functions are replaced with in-memory fakes. Run: python -m unittest (from the repo root)."""
from __future__ import annotations

import datetime as dt
import random
import unittest
from unittest import mock

from client_base_engine import engine, source
from client_base_engine.model import listing_from_row
from client_base_engine.org import Branch, Org
from client_base_engine.params import Params

NOW = dt.datetime(2026, 10, 18, 12, 0)
BRANCH = ("Acme", "1 High Street, London")
RIVAL = ("Rival Homes", "2 Low Road, London")


def _rows(seed: int = 7):
    """A branch book over 6 outcodes plus later listings by a rival: some relisted, some let, some untouched."""
    rnd = random.Random(seed)
    own, later = [], []
    for oc_i, oc in enumerate(["SE3", "SE10", "SE13", "SE12", "SE9", "SE18"]):
        for h in range(1, 8 + oc_i * 3):
            pc = f"{oc} {1 + h % 4}AB"
            addr = f"{h} Park Road, London, {pc}"
            uprn = str(1000 * (oc_i + 1) + h) if h % 3 else None
            sale = h % 2 == 0
            own.append({"property_identifier": f"o{oc}{h}", "listing_id": f"o{oc}{h}", "agent_name": BRANCH[0],
                        "agent_address": BRANCH[1], "full_property_address": addr, "postcode": pc, "uprn": uprn,
                        "listing_status": "sale" if sale else "rent", "status": "archived", "display_status": "",
                        "price": rnd.choice([350000, 500000, 725000]) if sale else rnd.choice([1500, 2100]),
                        "price_frequency": "monthly", "first_published_date": NOW - dt.timedelta(days=900 + h),
                        "update_time": NOW - dt.timedelta(days=800)})
            if h % 2:
                later.append({"property_identifier": f"r{oc}{h}", "listing_id": f"r{oc}{h}", "agent_name": RIVAL[0],
                              "agent_address": RIVAL[1], "full_property_address": addr, "postcode": pc, "uprn": uprn,
                              "listing_status": "sale" if sale else "rent", "status": "for_sale" if sale else "to_rent",
                              "display_status": "", "price": 510000 if sale else 1800, "price_frequency": "monthly",
                              "first_published_date": NOW - dt.timedelta(days=40 + h),
                              "update_time": NOW - dt.timedelta(days=3)})
    return own, later


class FakeDB:
    timings: list = []


def _run(slice_postcodes: int, progress=None):
    own, later = _rows()

    def resolve_pairs(db, pairs, label):
        return [{"id": 1, "agent_master_name": "Acme", "agent_name_rm": BRANCH[0], "address_rm": BRANCH[1],
                 "agent_name_zl": None, "address_zl": None, "matching_algo": "x"}], {BRANCH}, set()

    def branch_listings(db, portal, pairs, outcodes, label):
        return [listing_from_row(portal, r) for r in own] if portal == "rightmove" and pairs else []

    def address_master(db, postcodes, label):
        return [], []

    def by_pc(db, portal, postcodes, lookback, label):
        if portal != "rightmove":
            return [], []
        return [listing_from_row(portal, r) for r in later + own if r["postcode"] in set(postcodes)], []

    def by_uprn(db, portal, uprns, lookback, label):
        if portal != "rightmove":
            return [], []
        return [listing_from_row(portal, r) for r in later + own if r["uprn"] and r["uprn"] in set(uprns)], []

    org = Org(org="t", group="Acme", brands=["Acme"], branches=[Branch(brand="Acme", name="HQ", pairs=[BRANCH])],
              params=Params())
    db = FakeDB()
    db.timings = []
    with mock.patch.object(source, "resolve_pairs", resolve_pairs), \
            mock.patch.object(source, "branch_listings", branch_listings), \
            mock.patch.object(source, "address_master", address_master), \
            mock.patch.object(source, "listings_by_postcode", by_pc), \
            mock.patch.object(source, "listings_by_uprn", by_uprn):
        return engine.run_org(org, db, now=NOW, progress=progress, slice_postcodes=slice_postcodes)


def _key(e):
    return (e["property_key"], e["event"], str(e.get("event_date")), e.get("current_agent"), round(float(e.get("est_fee") or 0)))


class ProgressiveTest(unittest.TestCase):
    def test_sliced_run_equals_one_pass(self):
        one = _run(slice_postcodes=10_000)
        many = _run(slice_postcodes=2)
        self.assertEqual(one["branches"][0]["slices"], 1)
        self.assertGreater(many["branches"][0]["slices"], 3)
        self.assertEqual(sorted(map(_key, one["events"])), sorted(map(_key, many["events"])))
        self.assertEqual(sorted(r["property_key"] for r in one["book"]), sorted(r["property_key"] for r in many["book"]))
        self.assertTrue(any(e["event"].startswith("relisted_with_competitor") for e in one["events"]))
        for k in ("book_properties", "event_rows", "properties_with_later_listings", "book_uprns"):
            self.assertEqual(one["branches"][0][k], many["branches"][0][k], k)

    def test_progress_is_cumulative_and_biggest_outcode_first(self):
        seen = []
        _run(slice_postcodes=2, progress=lambda r: seen.append((dict(r["progress"]), len(r["book"]))))
        self.assertGreater(len(seen), 3)
        done = [p["listings_done"] for p, _ in seen]
        self.assertEqual(done, sorted(done))
        self.assertEqual(seen[-1][0]["listings_done"], seen[-1][0]["listings_total"])
        self.assertEqual(seen[-1][0]["outcodes_done"], seen[-1][0]["outcodes_total"])
        books = [n for _, n in seen]
        self.assertEqual(books, sorted(books))
        self.assertGreaterEqual(books[0], books[-1] / 6)   # the first slice is the biggest outcode (SE18)

    def test_a_failing_progress_callback_never_stops_the_run(self):
        def boom(_):
            raise RuntimeError("display failed")
        out = _run(slice_postcodes=2, progress=boom)
        self.assertTrue(out["events"])

    def test_plan_slices_never_splits_an_outcode(self):
        own, _ = _rows()
        ls = [listing_from_row("rightmove", r) for r in own]
        sl = engine.plan_slices(ls, 2)
        flat = [oc for s in sl for oc in s]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(flat[0], "SE18")   # most listings first


class TwinUprnTest(unittest.TestCase):
    def _l(self, portal, lid, addr, uprn, days=0, price=500000):
        return listing_from_row(portal, {"listing_id": lid, "agent_name": "Acme", "agent_address": "1 High St",
                                         "full_property_address": addr, "postcode": "SE10 8AA", "uprn": uprn,
                                         "listing_status": "sale", "status": "for_sale", "price": price,
                                         "first_published_date": NOW - dt.timedelta(days=30 + days)})

    def test_a_rightmove_listing_takes_its_zoopla_twins_uprn(self):
        rm = self._l("rightmove", "1", "72 The Coach House, Hardy Road, London SE10 8AA", None)
        zl = self._l("zoopla", "2", "The Coach House, 72 Hardy Road, London SE10 8AA", "100020972960", days=3)
        self.assertEqual(engine.adopt_twin_uprns([rm, zl]), 1)
        self.assertEqual(rm.uprn, "100020972960")

    def test_no_twin_when_price_date_or_portal_differ_or_ambiguous(self):
        rm = self._l("rightmove", "1", "Flat 1, X", None)
        far = self._l("zoopla", "2", "Flat 1, X", "11", days=40)
        other_price = self._l("zoopla", "3", "Flat 1, X", "12", price=510000)
        same_portal = self._l("rightmove", "4", "Flat 1, X", "13")
        self.assertEqual(engine.adopt_twin_uprns([rm, far, other_price, same_portal]), 0)
        a = self._l("zoopla", "5", "Flat 1, X", "21")
        b = self._l("zoopla", "6", "Flat 2, X", "22")
        self.assertEqual(engine.adopt_twin_uprns([rm, a, b]), 0)
        self.assertIsNone(rm.uprn)


if __name__ == "__main__":
    unittest.main()
