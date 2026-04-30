import unittest
from decimal import Decimal

from shopper_merge_bot.runtime import (
    build_offer_publish_text,
    build_offer_publish_text_from_body,
    stable_offer_body,
)


class RuntimeFormattingTest(unittest.TestCase):
    def test_offer_text_is_readable_and_keeps_required_fields(self) -> None:
        text = build_offer_publish_text(
            product="Friggitrice ad aria Ninja",
            original_price=Decimal("129.99"),
            current_price=Decimal("79.99"),
            offer_url="https://amazon.it/dp/B0ABCDEF12",
            category="casa",
            sources=[("Canale Offerte", "https://t.me/canale/1")],
            max_chars=1024,
        )

        self.assertIn("🔥 Offerta pronta da controllare", text)
        self.assertIn("📦 Prodotto: Friggitrice ad aria Ninja", text)
        self.assertIn("💸 Prezzo attuale: 79,99 EUR", text)
        self.assertIn("🏷️ Prezzo originale: 129,99 EUR", text)
        self.assertIn("✅ Risparmio stimato: 50,00 EUR (38%)", text)
        self.assertIn("🔗 Link offerta: https://amazon.it/dp/B0ABCDEF12", text)
        self.assertIn("📂 Categoria: casa", text)

    def test_merged_offer_shows_source_count_without_source_names(self) -> None:
        body = stable_offer_body(
            product="SSD Samsung",
            original_price=Decimal("99.90"),
            current_price=Decimal("59.90"),
            offer_url="https://amazon.it/dp/B0SSDTEST1",
        )

        text = build_offer_publish_text_from_body(
            body=body,
            category="elettronica",
            sources=[
                ("Canale Uno", "https://t.me/uno/1"),
                ("Canale Due", "https://t.me/due/2"),
            ],
            max_chars=1024,
        )

        self.assertIn("🔁 Confermata da 2 fonti", text)
        self.assertNotIn("Canale Uno", text)
        self.assertNotIn("Canale Due", text)
