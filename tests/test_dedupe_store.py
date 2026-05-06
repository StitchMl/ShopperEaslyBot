import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from shopper_merge_bot.dedupe import DedupeStore


class DedupeStoreTest(unittest.TestCase):
    def test_remove_source_deletes_saved_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                store.add_source("12345", "Shopper Easly Bot", "shoppereaslybot")
                self.assertIn("12345", store.source_ids())

                store.remove_source("12345")

                self.assertNotIn("12345", store.source_ids())
            finally:
                store.close()

    def test_save_offer_reactivates_deleted_offer_with_new_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                store.save_offer(
                    fingerprint="abc",
                    destination_chat_id="dest",
                    primary_message_id=1,
                    extra_message_ids=(),
                    text="old",
                    category="casa",
                    price=Decimal("10"),
                )
                store.mark_offer_status("abc", "deleted:legacy-format")

                store.save_offer(
                    fingerprint="abc",
                    destination_chat_id="dest",
                    primary_message_id=42,
                    extra_message_ids=(43,),
                    text="new",
                    category="casa",
                    price=Decimal("9"),
                )

                offer = store.get_offer("abc")
                self.assertIsNotNone(offer)
                assert offer is not None
                self.assertEqual(offer.status, "active")
                self.assertEqual(offer.primary_message_id, 42)
                self.assertEqual(offer.extra_message_ids, (43,))
                self.assertEqual(offer.text, "new")
            finally:
                store.close()

    def test_rename_and_merge_offer_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DedupeStore(Path(temp_dir) / "shopper.sqlite3")
            try:
                store.save_offer(
                    fingerprint="short-a",
                    destination_chat_id="dest",
                    primary_message_id=10,
                    extra_message_ids=(),
                    text="a",
                    category="altro",
                    price=Decimal("4.99"),
                )
                store.add_offer_source(
                    fingerprint="short-a",
                    source_chat_id="source-a",
                    source_message_id=1,
                    source_title="A",
                    source_link="",
                )
                store.save_offer(
                    fingerprint="short-b",
                    destination_chat_id="dest",
                    primary_message_id=11,
                    extra_message_ids=(),
                    text="b",
                    category="altro",
                    price=Decimal("4.99"),
                )
                store.add_offer_source(
                    fingerprint="short-b",
                    source_chat_id="source-b",
                    source_message_id=2,
                    source_title="B",
                    source_link="",
                )

                self.assertTrue(store.rename_offer_fingerprint("short-a", "canonical"))
                source_count = store.merge_offer_into("short-b", "canonical")

                self.assertEqual(source_count, 2)
                self.assertEqual(store.get_offer("canonical").status, "active")  # type: ignore[union-attr]
                self.assertEqual(
                    store.get_offer("short-b").status,  # type: ignore[union-attr]
                    "deleted:merged-duplicate",
                )
                self.assertEqual(len(store.offer_sources("canonical")), 2)
                source_messages = store.offer_source_messages("canonical")
                self.assertEqual(
                    [(source.source_chat_id, source.source_message_id) for source in source_messages],
                    [("source-a", 1), ("source-b", 2)],
                )
            finally:
                store.close()
