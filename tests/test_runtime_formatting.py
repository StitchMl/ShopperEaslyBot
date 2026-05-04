import unittest
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.tl.types import User

from shopper_merge_bot.dedupe import DedupeStore, OfferRecord
from shopper_merge_bot.runtime import (
    build_offer_publish_text,
    build_offer_publish_text_from_body,
    deleted_event_source_ids,
    delete_messages_with_fallback,
    edit_offer_message,
    fingerprint_for_offer_url,
    passes_filters,
    preferred_source_id,
    product_similarity,
    resolve_offer_urls,
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

    def test_product_similarity_ignores_minor_promo_suffixes(self) -> None:
        self.assertGreaterEqual(
            product_similarity(
                "Hello Kitty - Zainetto per Bambina",
                "Hello Kitty - Zainetto per Bambina, scende al Minimo Storico",
            ),
            0.78,
        )
        self.assertLess(
            product_similarity(
                "Hello Kitty - Zainetto per Bambina",
                "Bose QuietComfort - Auricolari Wireless",
            ),
            0.78,
        )


class RuntimeFilterTest(unittest.TestCase):
    def test_base_category_filter_matches_site_subcategory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                store.set_filter_categories(("libri",))
                self.assertTrue(passes_filters(store, "libri/thriller", Decimal("9.99")))
                self.assertFalse(passes_filters(store, "elettronica", Decimal("9.99")))
            finally:
                store.close()

    def test_deleted_event_source_ids_include_channel_peer_variant(self) -> None:
        self.assertEqual(deleted_event_source_ids(12345), ("12345", "-10012345"))
        self.assertEqual(deleted_event_source_ids(-10012345), ("-10012345",))
        self.assertEqual(
            preferred_source_id(("12345", "-10012345"), {"-10012345"}),
            "-10012345",
        )


class RuntimeUrlResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_shortlinks_resolve_before_original_url(self) -> None:
        with patch(
            "shopper_merge_bot.runtime.resolve_redirect_url",
            side_effect=lambda url: {
                "https://amzlink.to/a": "https://www.amazon.it/dp/B0D87L8GJ2?tag=one",
                "https://amzlink.to/b": "https://www.amazon.it/dp/B0D87L8GJ2?tag=two",
            }.get(url, url),
        ):
            first = await resolve_offer_urls(("https://amzlink.to/a",))
            second = await resolve_offer_urls(("https://amzlink.to/b",))

        self.assertEqual(first[0], "https://www.amazon.it/dp/B0D87L8GJ2?tag=one")
        self.assertEqual(second[0], "https://www.amazon.it/dp/B0D87L8GJ2?tag=two")
        self.assertEqual(
            fingerprint_for_offer_url(first[0]),
            fingerprint_for_offer_url(second[0]),
        )


class RuntimeBotApiCleanupTest(unittest.IsolatedAsyncioTestCase):
    def private_destination(self) -> User:
        return User(id=123456, is_self=False, bot=False, access_hash=0, first_name="Matteo")

    async def test_delete_private_destination_uses_bot_api(self) -> None:
        sender = AsyncMock()
        with patch(
            "shopper_merge_bot.runtime.bot_api_request",
            new=AsyncMock(return_value={"ok": True, "result": True}),
        ) as api_request:
            deleted = await delete_messages_with_fallback(
                [sender],
                self.private_destination(),
                [42],
                bot_token="token",
            )

        self.assertTrue(deleted)
        sender.delete_messages.assert_not_called()
        api_request.assert_awaited_once()

    async def test_delete_private_destination_does_not_fall_back_after_bot_api_failure(self) -> None:
        sender = AsyncMock()
        with patch(
            "shopper_merge_bot.runtime.bot_api_request",
            new=AsyncMock(return_value={"ok": False, "description": "Bad Request: message can't be deleted"}),
        ), self.assertLogs("shopper_merge_bot", level="WARNING"):
            deleted = await delete_messages_with_fallback(
                [sender],
                self.private_destination(),
                [42],
                bot_token="token",
            )

        self.assertFalse(deleted)
        sender.delete_messages.assert_not_called()

    async def test_edit_private_media_caption_uses_bot_api_caption_fallback(self) -> None:
        sender = AsyncMock()
        offer = OfferRecord(
            fingerprint="diagnose",
            destination_chat_id="123456",
            primary_message_id=42,
            extra_message_ids=(),
            text="old",
            category="altro",
            price=None,
            source_count=1,
            status="active",
        )
        api = AsyncMock(
            side_effect=[
                {"ok": False, "description": "Bad Request: there is no text in the message to edit"},
                {"ok": True, "result": True},
            ]
        )
        with patch("shopper_merge_bot.runtime.bot_api_request", new=api):
            edited = await edit_offer_message(
                [sender],
                self.private_destination(),
                offer,
                "new",
                bot_token="token",
            )

        self.assertTrue(edited)
        sender.edit_message.assert_not_called()
        self.assertEqual([call.args[1] for call in api.await_args_list], ["editMessageText", "editMessageCaption"])
