import unittest

from insta_reactor.automation.navigator import normalize_chat_name


class TestNormalizeChatName(unittest.TestCase):
    def test_strips_trailing_emojis(self):
        self.assertEqual(normalize_chat_name("freakhan😋💥🧽"), "freakhan")

    def test_plain_name_unchanged(self):
        self.assertEqual(normalize_chat_name("Vidur N Rao"), "Vidur N Rao")

    def test_no_substring_collapse(self):
        # emoji-stripping must NOT make distinct names collide
        self.assertNotEqual(normalize_chat_name("40fitandshyam"),
                            normalize_chat_name("shyam"))
        self.assertEqual(normalize_chat_name("40fitandshyam"), "40fitandshyam")

    def test_emoji_in_middle_and_symbols(self):
        self.assertEqual(normalize_chat_name("gang 🥀"), "gang")
        self.assertEqual(normalize_chat_name("pAvANi SRi✨✨"), "pAvANi SRi")

    def test_empty_and_none(self):
        self.assertEqual(normalize_chat_name(""), "")
        self.assertEqual(normalize_chat_name(None), "")


if __name__ == "__main__":
    unittest.main()
