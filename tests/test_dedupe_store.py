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
