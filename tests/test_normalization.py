import unittest

from shopper_merge_bot.normalization import (
    build_fingerprint,
    canonicalize_url,
    extract_urls,
    normalize_text,
)


class NormalizationTest(unittest.TestCase):
    def test_extract_urls_strips_trailing_punctuation(self) -> None:
        self.assertEqual(
            extract_urls("Vai su https://example.com/deal?utm_source=x!"),
            ["https://example.com/deal?utm_source=x"],
        )

    def test_canonicalize_url_removes_common_tracking(self) -> None:
        url = "HTTPS://Example.com/deal/?utm_source=telegram&b=2&a=1&fbclid=abc"
        self.assertEqual(canonicalize_url(url), "https://example.com/deal?a=1&b=2")

    def test_normalize_text_is_stable_for_accents_and_spacing(self) -> None:
        self.assertEqual(
            normalize_text("Offerta lampo: caffe' e Caffe EUR 9,99"),
            "offerta lampo caffe e caffe eur 9 99",
        )

    def test_fingerprint_ignores_tracking_params(self) -> None:
        first = build_fingerprint("https://example.com/a?utm_campaign=x&id=42", "a")
        second = build_fingerprint("https://example.com/a?id=42&utm_source=y", "b")
        self.assertEqual(first, second)

    def test_amazon_url_canonicalizes_to_asin(self) -> None:
        first = canonicalize_url(
            "https://www.amazon.it/Prodotto-Bello/dp/B0ABCDEF12?tag=foo-21&psc=1"
        )
        second = canonicalize_url(
            "https://amazon.it/gp/product/B0ABCDEF12?tag=bar-21&linkCode=abc"
        )
        self.assertEqual(first, "https://amazon.it/dp/B0ABCDEF12")
        self.assertEqual(first, second)
