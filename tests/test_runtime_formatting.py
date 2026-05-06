import unittest
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.tl.types import User

from shopper_merge_bot.dedupe import DedupeStore, OfferRecord
from shopper_merge_bot.site_context import OfferActivity
from shopper_merge_bot.runtime import (
    build_offer_publish_text,
    build_offer_publish_text_from_body,
    create_product_gif,
    deleted_event_source_ids,
    delete_messages_with_fallback,
    edit_offer_media_as_gif,
    edit_offer_message,
    fingerprint_for_offer_url,
    menu_group_summaries,
    passes_filters,
    preferred_source_id,
    product_similarity,
    render_offer_menu_detail_text,
    render_offer_menu_text,
    render_menu_index_text,
    purge_inactive_link_offers,
    purge_inactive_published_messages,
    set_publish_mode,
    resolve_offer_urls,
    stable_offer_body,
    offer_menu_key,
    parse_menu_callback_data,
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

    def test_create_product_gif_uses_unique_images(self) -> None:
        from PIL import Image, ImageSequence

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            first = temp_path / "first.png"
            duplicate = temp_path / "duplicate.png"
            second = temp_path / "second.png"
            output = temp_path / "product.gif"
            Image.new("RGB", (32, 32), "red").save(first)
            Image.new("RGB", (32, 32), "red").save(duplicate)
            Image.new("RGB", (32, 32), "blue").save(second)

            self.assertTrue(create_product_gif((first, duplicate, second), output))

            with Image.open(output) as image:
                self.assertEqual(sum(1 for _ in ImageSequence.Iterator(image)), 2)

    def test_offer_menu_groups_and_marks_recent_offers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                store.set_filter_categories(("elettronica", "software"))
                fingerprint = fingerprint_for_offer_url("https://amazon.it/dp/B0HEADSET1")
                store.save_offer(
                    fingerprint=fingerprint,
                    destination_chat_id="dest",
                    primary_message_id=0,
                    extra_message_ids=(),
                    text=stable_offer_body(
                        product="Bose QuietComfort Cuffie Wireless",
                        original_price=Decimal("299.99"),
                        current_price=Decimal("219.00"),
                        offer_url="https://amazon.it/dp/B0HEADSET1",
                    ),
                    category="elettronica",
                    price=Decimal("219.00"),
                )
                store.add_offer_source(
                    fingerprint=fingerprint,
                    source_chat_id="source",
                    source_message_id=1,
                    source_title="Canale",
                    source_link="",
                )
                offer = store.get_offer(fingerprint)
                assert offer is not None

                self.assertEqual(offer_menu_key(offer), "elettronica:cuffie")
                text = render_offer_menu_text(store, offer_menu_key(offer), [offer], 3500)

                self.assertIn("elettronica / Cuffie", text)
                self.assertIn("NUOVE 24h: 1", text)
                self.assertIn("[NUOVA] 1. Bose QuietComfort Cuffie Wireless", text)
                self.assertIn("https://amazon.it/dp/B0HEADSET1", text)
                index = render_menu_index_text(store)
                self.assertIn("Shopper Easly - Menu offerte", index)
                self.assertIn("Offerte attive: 1", index)
                self.assertIn("NUOVE 24h: 1", index)
                summaries = menu_group_summaries(store)
                self.assertIn(("software:all", "software / Software", 0, 0), summaries)
                empty_detail = render_offer_menu_detail_text(store, "software:all", [], 0, 3500)
                self.assertIn("Shopper Easly - software / Software", empty_detail)
                self.assertIn("Offerte attive: 0 | NUOVE 24h: 0", empty_detail)
                self.assertIn("Nessuna offerta attiva in questa tipologia.", empty_detail)
            finally:
                store.close()

    def test_publish_mode_normalizes_menu_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                self.assertEqual(set_publish_mode(store, "menu"), "menu-only")
            finally:
                store.close()

    def test_menu_callback_data_is_parsed(self) -> None:
        self.assertEqual(
            parse_menu_callback_data(b"menu:open:elettronica:cuffie:2"),
            ("open", "elettronica:cuffie", 2),
        )
        self.assertEqual(parse_menu_callback_data(b"menu:close"), ("close", "", 0))


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


class RuntimeExpiredOfferCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_purge_inactive_link_offer_deletes_message_and_marks_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                fingerprint = fingerprint_for_offer_url("https://example.com/product")
                store.save_offer(
                    fingerprint=fingerprint,
                    destination_chat_id="dest",
                    primary_message_id=42,
                    extra_message_ids=(),
                    text=stable_offer_body(
                        product="Prodotto test",
                        original_price=Decimal("20"),
                        current_price=Decimal("10"),
                        offer_url="https://example.com/product",
                    ),
                    category="casa",
                    price=Decimal("10"),
                )
                with patch(
                    "shopper_merge_bot.runtime.offer_activity_for_offer_urls",
                    new=AsyncMock(
                        return_value=OfferActivity(
                            url="https://example.com/product",
                            status="inactive",
                            reason="structured-availability",
                            fetched=True,
                        )
                    ),
                ), patch(
                    "shopper_merge_bot.runtime.delete_messages_with_fallback",
                    new=AsyncMock(return_value=True),
                ) as delete_messages:
                    scanned, deleted, active, unknown, failed = await purge_inactive_link_offers(
                        senders=[],
                        destination=object(),
                        store=store,
                        limit=10,
                    )

                self.assertEqual((scanned, deleted, active, unknown, failed), (1, 1, 0, 0, 0))
                self.assertEqual(store.get_offer(fingerprint).status, "deleted:expired-link:structured-availability")  # type: ignore[union-attr]
                delete_messages.assert_awaited_once()
            finally:
                store.close()

    async def test_purge_expired_scans_destination_messages_not_in_store(self) -> None:
        class FakeMessage:
            id = 77

            def __init__(self, text: str) -> None:
                self.raw_text = text

        class FakeReader:
            def __init__(self, message: FakeMessage) -> None:
                self.message = message

            def iter_messages(self, destination: object, limit=None):  # noqa: ANN001
                async def generate():
                    yield self.message

                return generate()

        text = build_offer_publish_text(
            product="Prodotto non tracciato",
            original_price=Decimal("20"),
            current_price=Decimal("10"),
            offer_url="https://example.com/product",
            category="casa",
            sources=[],
            max_chars=1024,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                with patch(
                    "shopper_merge_bot.runtime.resolve_offer_urls",
                    new=AsyncMock(return_value=("https://example.com/product",)),
                ), patch(
                    "shopper_merge_bot.runtime.offer_activity_for_offer_urls",
                    new=AsyncMock(
                        return_value=OfferActivity(
                            url="https://example.com/product",
                            status="inactive",
                            reason="price-increased:12.00>10.00",
                            fetched=True,
                            current_price=Decimal("12.00"),
                        )
                    ),
                ), patch(
                    "shopper_merge_bot.runtime.delete_messages_with_fallback",
                    new=AsyncMock(return_value=True),
                ) as delete_messages:
                    result = await purge_inactive_published_messages(
                        reader=FakeReader(FakeMessage(text)),
                        senders=[],
                        destination=object(),
                        store=store,
                        limit=100,
                    )

                self.assertEqual(result, (1, 1, 0, 0, 0))
                delete_messages.assert_awaited_once()
                self.assertEqual(delete_messages.await_args.args[2], [77])
            finally:
                store.close()


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


class RuntimeOfferMediaGifTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_media_updates_offer_as_gif(self) -> None:
        from PIL import Image

        class FakeMessage:
            media = object()

            def __init__(self, color: str, name: str) -> None:
                self.color = color
                self.name = name

            async def download_media(self, file: str) -> str:
                path = Path(file) / self.name
                Image.new("RGB", (32, 32), self.color).save(path)
                return str(path)

        class FakeSession:
            pass

        class FakeSender:
            session = FakeSession()

            def __init__(self) -> None:
                self.edited_file_exists = False
                self.edited_caption = ""

            async def get_messages(self, destination: object, ids: int) -> FakeMessage:
                return FakeMessage("red", "target.png")

            async def edit_message(self, destination: object, message_id: int, text: str, **kwargs: object) -> None:
                self.edited_caption = text
                self.edited_file_exists = Path(kwargs["file"]).exists()  # type: ignore[arg-type]

        offer = OfferRecord(
            fingerprint="product",
            destination_chat_id="dest",
            primary_message_id=42,
            extra_message_ids=(),
            text="old",
            category="elettronica",
            price=Decimal("10"),
            source_count=1,
            status="active",
        )
        sender = FakeSender()

        updated = await edit_offer_media_as_gif(
            [sender], object(), offer, FakeMessage("blue", "source.png"), "caption"
        )

        self.assertTrue(updated)
        self.assertTrue(sender.edited_file_exists)
        self.assertEqual(sender.edited_caption, "caption")
