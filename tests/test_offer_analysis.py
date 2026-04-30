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
        self.assertEqual(extract_price("SSD Samsung a 39,99 EUR"), Decimal("39.99"))
        self.assertIsNone(extract_price("Canale con 5047 iscritti"))

    def test_classify_category(self) -> None:
        self.assertEqual(classify_category("Offerta cuffie bluetooth Sony"), "elettronica")

    def test_invalid_offer_detection(self) -> None:
        self.assertTrue(is_invalid_offer("Offerta scaduta, non piu disponibile"))
        self.assertFalse(is_invalid_offer("Offerta lampo ancora attiva"))

    def test_source_score(self) -> None:
        self.assertGreaterEqual(source_score("Junction Bot", "junctionbot", source_type="bot"), 2)
        self.assertGreaterEqual(
            source_score("Offerte Amazon", "deals_channel", source_type="channel"),
            3,
        )
        self.assertGreaterEqual(source_score("Notizie Tech", "newsbot", "notizie", "bot"), 4)

    def test_analyze_offer(self) -> None:
        facts = analyze_offer("Friggitrice ad aria a 49,90 EUR")
        self.assertEqual(facts.category, "casa")
        self.assertEqual(facts.price, Decimal("49.90"))
        self.assertFalse(facts.invalid)
        self.assertFalse(facts.complete)

    def test_complete_offer_requires_product_prices_and_link(self) -> None:
        facts = analyze_offer(
            "Friggitrice ad aria Ninja\nDa 129,99 EUR a 79,99 EUR\nCompra qui",
            ("https://www.amazon.it/Ninja-Friggitrice/dp/B0ABCDEF12?tag=x",),
        )
        self.assertTrue(facts.complete)
        self.assertEqual(facts.product, "Friggitrice ad aria Ninja")
        self.assertEqual(facts.original_price, Decimal("129.99"))
        self.assertEqual(facts.current_price, Decimal("79.99"))
        self.assertEqual(facts.offer_url, "https://amazon.it/dp/B0ABCDEF12")

    def test_channel_promo_is_incomplete(self) -> None:
        facts = analyze_offer(
            "OFFERTA TOP SU PRODOTTI FITNESS BELLEZZA E BENESSERE\n"
            "Iscriviti gratis al canale"
        )
        self.assertFalse(facts.complete)
