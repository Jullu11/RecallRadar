from __future__ import annotations

import unittest

from app.matcher import ProductCheck, match_product, query_matches_demo_notice


class MatcherTests(unittest.TestCase):
    def test_affected_exact_code(self) -> None:
        result = match_product(ProductCheck(model="AO10FK-A", date_code="1523"))
        self.assertEqual(result.outcome, "affected")

    def test_affected_year_suffix(self) -> None:
        result = match_product(ProductCheck(model="ao10fk-b", date_code="1221"))
        self.assertEqual(result.outcome, "affected")

    def test_wrong_lot(self) -> None:
        result = match_product(ProductCheck(model="AO10FK-A", date_code="1524"))
        self.assertEqual(result.outcome, "no_match")

    def test_wrong_model(self) -> None:
        result = match_product(ProductCheck(model="WRONG-1", date_code="1523"))
        self.assertEqual(result.outcome, "no_match")

    def test_missing_identifiers(self) -> None:
        result = match_product(ProductCheck(model="AO10FK-A"))
        self.assertEqual(result.outcome, "possible_match")
        self.assertIn("date code", " ".join(result.missing_identifiers).casefold())

    def test_query_matches_demo(self) -> None:
        self.assertTrue(query_matches_demo_notice("myCharge POWER HUB charger"))
        self.assertFalse(query_matches_demo_notice("random blender"))


if __name__ == "__main__":
    unittest.main()
