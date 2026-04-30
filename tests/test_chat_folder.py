import unittest

from shopper_merge_bot.chat_folder import extract_chat_folder_slug


class ChatFolderTest(unittest.TestCase):
    def test_extract_slug_from_tme_link(self) -> None:
        self.assertEqual(
            extract_chat_folder_slug("https://t.me/addlist/abcDEF123"),
            "abcDEF123",
        )

    def test_extract_slug_from_tg_link(self) -> None:
        self.assertEqual(
            extract_chat_folder_slug("tg://addlist?slug=abcDEF123"),
            "abcDEF123",
        )

    def test_extract_slug_from_bare_value(self) -> None:
        self.assertEqual(extract_chat_folder_slug("abcDEF123"), "abcDEF123")
