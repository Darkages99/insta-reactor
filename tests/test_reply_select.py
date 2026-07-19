import unittest

from insta_reactor.models import (
    Comment, Profile, Settings, ReelContext, Emotion, ReplyStyle,
)
from insta_reactor.engine.reply_select import select_reply


def ctx(comments):
    return ReelContext(chat_name="T", reel_id="r", comments=comments,
                       comment_count=len(comments))


class TestReplySelect(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.profile = Profile(emoji_prefs=["💀", "😭", "😂"],
                               common_replies=["bro 💀", "nah 😭"],
                               reply_style=ReplyStyle.SINGLE)

    def test_very_popular_sent_verbatim(self):
        comments = [Comment("nah this is crazy 😭", likes=1200)] + \
                   [Comment("😂", likes=1)] * 30
        text, src = select_reply(ctx(comments), Emotion.CRYING,
                                 self.profile, self.settings)
        self.assertEqual(src, "popular_verbatim")
        self.assertEqual(text, "nah this is crazy 😭")

    def test_popular_by_share(self):
        # one comment owns the vast majority of a modest like pool
        comments = [Comment("goes hard 🔥", likes=40)] + \
                   [Comment("ok", likes=1)] * 5
        text, src = select_reply(ctx(comments), Emotion.FIRE,
                                 self.profile, self.settings)
        self.assertEqual(src, "popular_verbatim")
        self.assertEqual(text, "goes hard 🔥")

    def test_long_popular_comment_not_echoed(self):
        long_txt = "x" * 200
        comments = [Comment(long_txt, likes=1000)] + [Comment("💀")] * 20
        _, src = select_reply(ctx(comments), Emotion.DEAD,
                              self.profile, self.settings)
        self.assertNotEqual(src, "popular_verbatim")

    def test_favourite_emoji_in_comments_wins(self):
        # no likes -> no verbatim; crowd uses 💀 which is your top favourite
        comments = [Comment("💀")] * 10 + [Comment("😂")] * 10
        text, src = select_reply(ctx(comments), Emotion.DEAD,
                                 self.profile, self.settings)
        self.assertEqual(src, "favourite_match")
        self.assertEqual(text, "💀")

    def test_favourite_prefers_winner_emotion(self):
        # both 💀(DEAD) and 😭(CRYING) are favourites present in comments;
        # winner is CRYING so we should reply 😭 even though 💀 ranks higher.
        comments = [Comment("💀 😭")] * 12
        text, src = select_reply(ctx(comments), Emotion.CRYING,
                                 self.profile, self.settings)
        self.assertEqual(src, "favourite_match")
        self.assertEqual(text, "😭")

    def test_fallback_to_emotion(self):
        # no likes, no favourite emoji present -> emotion-styled build
        comments = [Comment("this is wild")] * 25
        text, src = select_reply(ctx(comments), Emotion.SHOCK,
                                 self.profile, self.settings)
        self.assertEqual(src, "emotion")
        self.assertEqual(text, "😱")

    def test_media_comment_never_echoed(self):
        # the most-liked "comment" is a GIF placeholder -> must be ignored
        comments = [Comment("GIF", likes=5000)] + [Comment("💀")] * 20
        _, src = select_reply(ctx(comments), Emotion.DEAD,
                              self.profile, self.settings)
        self.assertNotEqual(src, "popular_verbatim")

    def test_question_comment_not_echoed(self):
        # A specific question/reference about the video's content is
        # commentary, not a generic reaction — must not be echoed verbatim
        # even if short and popular.
        comments = [Comment("Graduation??? Kanye reference?", likes=200)] + \
                   [Comment("😂", likes=5)] * 20
        _, src = select_reply(ctx(comments), Emotion.LAUGH,
                              self.profile, self.settings)
        self.assertNotEqual(src, "popular_verbatim")

    def test_wordy_no_emoji_comment_not_echoed(self):
        # Short and popular, but pure text with no emoji at all — still a
        # specific worded take, not a generic reaction. Must fall through so
        # a favourite emoji present in the crowd (😂) wins instead.
        comments = [Comment("How many times bro", likes=90)] + \
                   [Comment("😂", likes=5)] * 20
        text, src = select_reply(ctx(comments), Emotion.LAUGH,
                                 self.profile, self.settings)
        self.assertNotEqual(src, "popular_verbatim")

    def test_moderately_long_comment_not_echoed(self):
        # Long enough to be a specific take (or spam), not a short generic
        # reaction — must fall through even though it's well under the old
        # 60-char limit.
        comments = [Comment("your ankle will give you an unforgettable "
                            "gift tomorrow 🔥", likes=80)] + \
                   [Comment("🔥", likes=5)] * 20
        _, src = select_reply(ctx(comments), Emotion.FIRE,
                              self.profile, self.settings)
        self.assertNotEqual(src, "popular_verbatim")


if __name__ == "__main__":
    unittest.main()
