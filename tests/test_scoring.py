import unittest

from insta_reactor.models import Comment, Profile, Settings, Emotion, ReplyStyle
from insta_reactor.engine import scoring


class TestLikeWeight(unittest.TestCase):
    def test_none_mode(self):
        self.assertEqual(scoring.like_weight(1000, "none"), 1.0)

    def test_log1p_grows_slowly(self):
        w0 = scoring.like_weight(0, "log1p")
        w100 = scoring.like_weight(100, "log1p")
        self.assertEqual(w0, 1.0)
        self.assertGreater(w100, w0)
        self.assertLess(w100, 7)  # bounded-ish


class TestPublicScores(unittest.TestCase):
    def test_weighting_and_cap(self):
        s = Settings(per_comment_cap=3, like_weight_mode="none")
        comments = [
            Comment("💀💀💀💀💀", 0),   # 5 skulls, capped to 3
            Comment("😭", 0),
        ]
        raw = scoring.public_scores(comments, s)
        self.assertEqual(raw[Emotion.DEAD], 3)
        self.assertEqual(raw[Emotion.CRYING], 1)

    def test_likes_increase_weight(self):
        s = Settings(like_weight_mode="log1p")
        raw = scoring.public_scores([Comment("💀", 500)], s)
        self.assertGreater(raw[Emotion.DEAD], 1.0)


class TestPersonalScores(unittest.TestCase):
    def test_primary_dominates(self):
        p = Profile(emoji_prefs=["💀", "😭", "😂"])
        norm = scoring._normalize(scoring.personal_scores(p))
        self.assertEqual(max(norm, key=norm.get), Emotion.DEAD)
        self.assertGreater(norm[Emotion.DEAD], norm[Emotion.CRYING])

    def test_common_replies_contribute(self):
        p = Profile(emoji_prefs=["💀"], common_replies=["nah 😭", "😭"])
        raw = scoring.personal_scores(p)
        self.assertIn(Emotion.CRYING, raw)


class TestConfidence(unittest.TestCase):
    def test_monotonic_in_share(self):
        s = Settings()
        low = scoring.confidence_score(0.3, 0.1, 20, s)
        high = scoring.confidence_score(0.7, 0.3, 40, s)
        self.assertLess(low, high)
        self.assertLessEqual(high, 1.0)


class TestBuildReply(unittest.TestCase):
    def test_single_uses_your_emoji(self):
        p = Profile(emoji_prefs=["💀"], reply_style=ReplyStyle.SINGLE)
        self.assertEqual(scoring.build_reply(Emotion.DEAD, p), "💀")

    def test_double(self):
        p = Profile(emoji_prefs=["💀"], reply_style=ReplyStyle.DOUBLE)
        self.assertEqual(scoring.build_reply(Emotion.DEAD, p), "💀💀")

    def test_text_emoji_reuses_common_reply(self):
        p = Profile(emoji_prefs=["💀"], common_replies=["bro 💀", "nah 😭"],
                    reply_style=ReplyStyle.TEXT_EMOJI)
        self.assertEqual(scoring.build_reply(Emotion.DEAD, p), "bro 💀")
        self.assertEqual(scoring.build_reply(Emotion.CRYING, p), "nah 😭")


if __name__ == "__main__":
    unittest.main()
