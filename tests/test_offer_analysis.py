import unittest
from decimal import Decimal

from shopper_merge_bot.offer_analysis import (
    analyze_offer,
    classify_category,
    extract_price,
    is_invalid_offer,
    source_score,
)


class OfferAnalysisTest(unittest.TestCase):
    def test_extract_price_requires_currency_marker(self) -> None:
        self.assertEqual(extract_price("SSD Samsung a 39,99€"), Decimal("39.99"))
        self.assertIsNone(extract_price("Canale con 5047 iscritti"))

    def test_classify_category(self) -> None:
        self.assertEqual(classify_category("Offerta cuffie bluetooth Sony"), "elettronica")

    def test_invalid_offer_detection(self) -> None:
        self.assertTrue(is_invalid_offer("Offerta scaduta, non piu disponibile"))
        self.assertFalse(is_invalid_offer("Offerta lampo ancora attiva"))

    def test_source_score(self) -> None:
        self.assertGreaterEqual(source_score("Junction Bot", "junctionbot"), 1)
        self.assertGreaterEqual(source_score("Offerte Amazon", "deals_channel"), 2)
        self.assertGreaterEqual(source_score("Notizie Tech", "newsbot", "notizie"), 3)

    def test_analyze_offer(self) -> None:
        facts = analyze_offer("Friggitrice ad aria a 49,90€")
        self.assertEqual(facts.category, "casa")
        self.assertEqual(facts.price, Decimal("49.90"))
        self.assertFalse(facts.invalid)
