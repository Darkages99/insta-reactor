import unittest

from insta_reactor.models import Emotion
from insta_reactor.engine.normalize import (
    extract_emojis, signals_for_comment,
)


class TestEmojiExtraction(unittest.TestCase):
    def test_basic_emojis(self):
        self.assertEqual(extract_emojis("lol 💀💀"), ["💀", "💀"])

    def test_variation_selector_stripped(self):
        # red heart with VS16 should normalize to base heart key
        self.assertEqual(extract_emojis("❤️"), ["❤"])

    def test_no_emoji(self):
        self.assertEqual(extract_emojis("just text"), [])


class TestSlangSignals(unittest.TestCase):
    def sig(self, text):
        return signals_for_comment(text)

    def test_skull_emoji_is_dead(self):
        self.assertEqual(self.sig("💀")[Emotion.DEAD], 1)

    def test_laughter_family(self):
        for t in ["lmao", "LMAOOO", "lmfaooo", "hahaha", "lolll", "rofl"]:
            with self.subTest(t=t):
                self.assertGreaterEqual(self.sig(t)[Emotion.LAUGH], 1)

    def test_elongation_collapse(self):
        # "deaaad" -> "dead" (3+ repeats collapse; 2 repeats are left alone)
        self.assertEqual(self.sig("bro im deaaad")[Emotion.DEAD], 1)

    def test_multiword_phrase_beats_word(self):
        s = self.sig("this goes hard ngl")
        self.assertEqual(s[Emotion.FIRE], 1)

    def test_crying(self):
        self.assertGreaterEqual(self.sig("im crying 😭")[Emotion.CRYING], 2)

    def test_mixed_comment(self):
        s = self.sig("LMAOO 💀💀 bro im deceased")
        self.assertEqual(s[Emotion.DEAD], 3)   # 2 skulls + "deceased"
        self.assertEqual(s[Emotion.LAUGH], 1)  # lmaoo

    def test_unknown_emoji_kept_as_own_key(self):
        s = self.sig("🦧")
        self.assertIn("🦧", s)

    def test_extra_slang(self):
        s = signals_for_comment("that was jokes", {"jokes": Emotion.LAUGH})
        self.assertEqual(s[Emotion.LAUGH], 1)


if __name__ == "__main__":
    unittest.main()
