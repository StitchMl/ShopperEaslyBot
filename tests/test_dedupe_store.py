import tempfile
import unittest
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
