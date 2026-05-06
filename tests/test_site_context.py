import unittest
from decimal import Decimal
from urllib.error import HTTPError
from unittest.mock import patch

from shopper_merge_bot.site_context import extract_prices_from_text, offer_activity_for_url, url_category_hint


class SiteContextTest(unittest.TestCase):
    def test_amazon_numeric_dp_is_book_hint(self) -> None:
        self.assertIn("libri", url_category_hint("https://www.amazon.it/dp/8857247597"))

    def test_amazon_regular_asin_has_no_book_hint(self) -> None:
        self.assertEqual(url_category_hint("https://www.amazon.it/dp/B0CZRW64BK"), "")

    def test_extract_price_does_not_split_spaced_amazon_fraction(self) -> None:
        self.assertEqual(
            extract_prices_from_text("21,12 EUR 21 , 12 €"),
            [Decimal("21.12")],
        )

    def test_offer_activity_uses_structured_out_of_stock(self) -> None:
        document = """
        <script type="application/ld+json">
        {"@type":"Product","offers":{"@type":"Offer","availability":"https://schema.org/OutOfStock"}}
        </script>
        """
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url("https://example.com/product")

        self.assertEqual(activity.status, "inactive")
        self.assertEqual(activity.reason, "structured-availability")

    def test_offer_activity_uses_structured_in_stock(self) -> None:
        document = """
        <script type="application/ld+json">
        {"@type":"Product","offers":{"@type":"Offer","availability":"https://schema.org/InStock"}}
        </script>
        """
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url("https://example.com/product")

        self.assertEqual(activity.status, "active")

    def test_offer_activity_keeps_mixed_structured_signals_unknown(self) -> None:
        document = """
        <script type="application/ld+json">
        [
          {"@type":"Product","offers":{"@type":"Offer","availability":"https://schema.org/InStock"}},
          {"@type":"Product","offers":{"@type":"Offer","availability":"https://schema.org/OutOfStock"}}
        ]
        </script>
        """
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url("https://example.com/product")

        self.assertEqual(activity.status, "unknown")

    def test_offer_activity_uses_prominent_unavailable_text(self) -> None:
        document = "<div id='availability'>Attualmente non disponibile.</div>"
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url("https://example.com/product")

        self.assertEqual(activity.status, "inactive")
        self.assertEqual(activity.reason, "page-availability")

    def test_offer_activity_uses_no_featured_offer_text(self) -> None:
        document = "<main>Nessuna offerta in evidenza disponibile per questo prodotto.</main>"
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url("https://www.amazon.it/dp/B00C1DIRXQ")

        self.assertEqual(activity.status, "inactive")
        self.assertEqual(activity.reason, "page-availability")

    def test_offer_activity_marks_price_increase_as_inactive(self) -> None:
        document = """
        <script type="application/ld+json">
        {"@type":"Product","offers":{"@type":"Offer","availability":"https://schema.org/InStock","price":"39.99"}}
        </script>
        """
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url(
                "https://example.com/product",
                expected_price=Decimal("19.15"),
            )

        self.assertEqual(activity.status, "inactive")
        self.assertEqual(activity.reason, "price-increased:39.99>19.15")
        self.assertEqual(activity.current_price, Decimal("39.99"))

    def test_offer_activity_keeps_matching_price_active(self) -> None:
        document = '<span id="priceblock_dealprice">19,15 EUR</span>'
        with patch("shopper_merge_bot.site_context.fetch_html", return_value=document):
            activity = offer_activity_for_url(
                "https://example.com/product",
                expected_price=Decimal("19.15"),
            )

        self.assertEqual(activity.status, "active")
        self.assertEqual(activity.reason, "price-match")

    def test_offer_activity_uses_amazon_mobile_fallback_for_price(self) -> None:
        desktop_document = "<title>Amazon.it</title>"
        mobile_document = '<span id="priceblock_ourprice">15,60 EUR</span>'
        with patch(
            "shopper_merge_bot.site_context.fetch_html",
            side_effect=[desktop_document, mobile_document],
        ):
            activity = offer_activity_for_url(
                "https://www.amazon.it/dp/B0CWH3KWHM",
                expected_price=Decimal("12.60"),
            )

        self.assertEqual(activity.status, "inactive")
        self.assertEqual(activity.reason, "amazon-mobile:price-increased:15.60>12.60")
        self.assertEqual(activity.current_price, Decimal("15.60"))

    def test_offer_activity_404_is_inactive(self) -> None:
        error = HTTPError("https://example.com/product", 404, "Not found", {}, None)
        with patch("shopper_merge_bot.site_context.fetch_html", side_effect=error):
            activity = offer_activity_for_url("https://example.com/product")

        self.assertEqual(activity.status, "inactive")
        self.assertEqual(activity.reason, "http-404")
