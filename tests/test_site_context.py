import unittest

from shopper_merge_bot.site_context import url_category_hint


class SiteContextTest(unittest.TestCase):
    def test_amazon_numeric_dp_is_book_hint(self) -> None:
        self.assertIn("libri", url_category_hint("https://www.amazon.it/dp/8857247597"))

    def test_amazon_regular_asin_has_no_book_hint(self) -> None:
        self.assertEqual(url_category_hint("https://www.amazon.it/dp/B0CZRW64BK"), "")
