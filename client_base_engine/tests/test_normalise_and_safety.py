"""Address key, postcode, rent and name normalisation, and the read-only query guard (no database)."""
from __future__ import annotations

import unittest

from client_base_engine.db import check_sql, hint
from client_base_engine.model import listing_from_row
from client_base_engine.normalise import address_key, address_postcode, canon_postcode, first_three_words, monthly_rent
from client_base_engine.org import name_has_prefix
from client_base_engine.params import Params


class AddressKey(unittest.TestCase):
    def test_matches_agent_report_sql(self):
        # SUBSTRING_INDEX(REPLACE(REPLACE(REPLACE(UPPER(REPLACE(REPLACE(a,', ',' '),' and parking space','')),
        #                 'FLAT ',''),'APARTMENT ',''),'UNIT ',''),' ',3)
        self.assertEqual(first_three_words("Flat 3, 12 Bennett Park, London, SE3 9RB"), "3 12 BENNETT")
        self.assertEqual(first_three_words("Apartment 9, Bowden House, 9 Palmer Road"), "9 BOWDEN HOUSE")
        self.assertEqual(first_three_words("12 Lee Road and parking space, London"), "12 LEE ROAD")

    def test_postcode_from_address_first(self):
        self.assertEqual(address_postcode("1 Lee Road, London, se3 9rb", "SE3 9ZZ"), "SE3 9RB")
        self.assertEqual(address_postcode("1 Lee Road, London", "se39rb"), "SE3 9RB")
        self.assertEqual(canon_postcode("sw1a1aa"), "SW1A 1AA")

    def test_key_joins_words_and_postcode(self):
        self.assertEqual(address_key("Flat 3, 12 Bennett Park, London, SE3 9RB", "SE3 9RB"), "3 12 BENNETT|SE3 9RB")


class Rent(unittest.TestCase):
    def test_frequencies(self):
        self.assertEqual(monthly_rent(500, None, "weekly"), round(500 * 52 / 12))
        self.assertEqual(monthly_rent(24000, None, "yearly"), 2000)
        self.assertEqual(monthly_rent(1500, None, "monthly"), 1500)
        self.assertEqual(monthly_rent(9999, 1750, "weekly"), 1750)          # price_per_month wins
        self.assertIsNone(monthly_rent(50, None, "monthly"))                 # implausible

    def test_listing_from_row(self):
        l = listing_from_row("zoopla", {"listing_id": 7, "listing_status": "rent", "price": 400, "price_frequency": "weekly",
                                        "full_property_address": "1 A Road, London, SE3 9AB", "postcode": "SE3 9AB",
                                        "status": "EXPIRED", "display_status": "Let", "uprn": 123})
        self.assertEqual((l.listing_type, l.price, l.uprn, l.is_agreed()), ("let", round(400 * 52 / 12), "123", True))
        s = listing_from_row("rightmove", {"listing_id": 8, "listing_status": "new-homes", "price": 300000,
                                           "full_property_address": "2 B Road, SE3 9AB", "status": "for_sale"})
        self.assertTrue(s.is_sale)


class Names(unittest.TestCase):
    def test_prefix_on_word_boundary(self):
        self.assertTrue(name_has_prefix("eXp UK, North London", "eXp"))
        self.assertFalse(name_has_prefix("Express Estate Agency", "eXp"))
        self.assertTrue(name_has_prefix("Winkworth - Blackheath", "Winkworth"))


class Guard(unittest.TestCase):
    def test_refuses_uncapped_and_writes(self):
        with self.assertRaises(ValueError):
            check_sql("SELECT * FROM t")
        with self.assertRaises(ValueError):
            check_sql("/*+ MAX_EXECUTION_TIME(1000) */ SELECT 1")             # hint before SELECT is ignored by MySQL
        with self.assertRaises(ValueError):
            check_sql("SELECT /*+ MAX_EXECUTION_TIME(90000) */ 1")            # over the 60s rule
        with self.assertRaises(ValueError):
            check_sql(f"SELECT {hint()} 1; DELETE FROM t")
        check_sql(f"SELECT {hint()} update_time, listing_update_date FROM t WHERE x = 'DELETE'")

    def test_params_reject_unknown(self):
        with self.assertRaises(ValueError):
            Params.from_dict({"nope": 1})
        self.assertEqual(Params.from_dict({"let_fee_pct": 10}).let_fee_pct, 10.0)


class BookFallbackTest(unittest.TestCase):
    def test_a_timed_out_book_query_is_read_in_year_windows(self):
        from client_base_engine import source
        from client_base_engine.db import QueryTimeout

        class DB:
            def __init__(self):
                self.calls = []

            def fetch(self, sql, params=None, label=""):
                self.calls.append(label)
                if len(self.calls) == 1:
                    raise QueryTimeout(label)
                return [{"listing_id": str(len(self.calls)), "full_property_address": "1 A Road, London SE3 9AA",
                         "postcode": "SE3 9AA", "listing_status": "sale", "price": 1}] if "2024-2025" in label else []
        db = DB()
        out = source.branch_listings(db, "rightmove", {("X", "Y")}, [], "x")
        self.assertEqual(len(out), 1)
        self.assertGreater(len(db.calls), 5)


class PoweredByTest(unittest.TestCase):
    def test_a_networks_own_agents_are_sister_offices(self):
        from client_base_engine.org import name_has_prefix
        self.assertTrue(name_has_prefix("Cerrone Estates, Powered by Exp UK", "eXp UK"))
        self.assertTrue(name_has_prefix("Billingham & Co, Powered by eXp UK", "eXp UK"))
        self.assertFalse(name_has_prefix("Powered by eXpress Homes", "eXp"))
        self.assertTrue(name_has_prefix("eXp UK, North East", "eXp UK"))


if __name__ == "__main__":
    unittest.main()


class TestAnonAddress(unittest.TestCase):
    def test_house_numbers_and_names_are_dropped(self):
        from client_base_engine.explorer import mask_address
        self.assertEqual(mask_address("Flat 3, 30 Pont Street, London"), "Pont Street, London")
        self.assertEqual(mask_address("13A Chartfield Avenue"), "Chartfield Avenue")
        self.assertEqual(mask_address("34, Drummond Place, Edinburgh"), "Drummond Place, Edinburgh")
        self.assertEqual(mask_address("The Manse, Peverell Terrace, Porthleven"), "Peverell Terrace, Porthleven")
        self.assertEqual(mask_address(""), "")

    def test_floor_descriptions_are_dropped(self):
        from client_base_engine.explorer import mask_address
        self.assertEqual(mask_address("Ground Floor 1, Bushnell Road, Wandsworth, London"), "Bushnell Road, Wandsworth, London")
        self.assertEqual(mask_address("Top Floor Flat, 12 Louisville Road"), "Louisville Road")

