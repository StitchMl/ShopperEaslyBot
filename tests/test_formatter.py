import unittest

from shopper_merge_bot.formatter import build_outbound_text, trim_text


class FormatterTest(unittest.TestCase):
    def test_trim_text_adds_marker(self) -> None:
        self.assertEqual(trim_text("abcdef", 5), "a ...")

    def test_build_outbound_text_includes_source_and_link(self) -> None:
        text = build_outbound_text(
            source_title="Deals",
            body="Sconto 50%",
            source_link="https://t.me/deals/1",
            max_chars=200,
        )
        self.assertEqual(
            text,
            "Da: Deals\n\nSconto 50%\n\nFonte: https://t.me/deals/1",
        )
