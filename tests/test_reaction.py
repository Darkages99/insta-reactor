import unittest

from insta_reactor.models import (
    Comment, Profile, Settings, ReelContext, Emotion, Action, FlagKind,
    ReplyStyle,
)
from insta_reactor.engine.reaction import decide_reaction


def profile():
    return Profile(emoji_prefs=["💀", "😭", "😂"],
                   common_replies=["bro 💀", "nah 😭", "LMAOO", "😭", "💀"],
                   reply_style=ReplyStyle.SINGLE)


def make(comments, **kw):
    base = dict(chat_name="T", reel_id="r", comments=comments,
                comment_count=kw.pop("comment_count", None))
    base.update(kw)
    return ReelContext(**base)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.p = profile()
        self.s = Settings()

    def test_rule1_preceding_text_flags(self):
        ctx = make([Comment("💀")] * 30, has_preceding_text=True,
                   preceding_text="this is literally you")
        d = decide_reaction(ctx, self.p, self.s)
        self.assertEqual(d.action, Action.FLAG)
        self.assertEqual(d.flag.kind, FlagKind.CONTEXT_TEXT)

    def test_unable_to_read(self):
        ctx = make(None, read_error=True)
        d = decide_reaction(ctx, self.p, self.s)
        self.assertEqual(d.flag.kind, FlagKind.UNABLE_TO_READ)

    def test_rule2_too_few_comments(self):
        ctx = make([Comment("💀")] * 8)
        d = decide_reaction(ctx, self.p, self.s)
        self.assertEqual(d.flag.kind, FlagKind.TOO_FEW_COMMENTS)

    def test_no_consensus_split(self):
        comments = ([Comment("💀")] * 5 + [Comment("😭")] * 5 +
                    [Comment("😂")] * 5 + [Comment("🔥")] * 5)
        d = decide_reaction(make(comments), self.p, self.s)
        self.assertEqual(d.action, Action.FLAG)
        self.assertEqual(d.flag.kind, FlagKind.NO_CONSENSUS)

    def test_no_recognizable_reactions(self):
        comments = [Comment("first"), Comment("nice video")] * 15
        d = decide_reaction(make(comments), self.p, self.s)
        self.assertEqual(d.flag.kind, FlagKind.NO_CONSENSUS)


class TestAutoReply(unittest.TestCase):
    def setUp(self):
        self.p = profile()
        self.s = Settings()

    def test_strong_dead_consensus_autoreplies(self):
        comments = ([Comment("💀", 40)] * 14 + [Comment("😭")] * 6 +
                    [Comment("😂")] * 4 + [Comment("🔥")] * 2)
        d = decide_reaction(make(comments), self.p, self.s)
        self.assertEqual(d.action, Action.AUTO_REPLY, d.flag)
        self.assertEqual(d.winning_emotion, Emotion.DEAD)
        self.assertEqual(d.reply_text, "💀")
        self.assertGreaterEqual(d.confidence, self.s.auto_reply_min_confidence)

    def test_personal_bias_flips_public_winner(self):
        # Public clearly leans 😂 (LAUGH), but my primary is 💀 (DEAD).
        # Spec: prefer my style when both plausible.
        comments = ([Comment("😂")] * 20 + [Comment("💀")] * 10 +
                    [Comment("🔥")] * 5 + [Comment("😭")] * 5)
        p = Profile(emoji_prefs=["💀"], common_replies=[],
                    reply_style=ReplyStyle.SINGLE)
        d = decide_reaction(make(comments), p, self.s)
        self.assertEqual(d.action, Action.AUTO_REPLY, d.flag)
        self.assertEqual(d.breakdown.public_winner, Emotion.LAUGH)
        self.assertEqual(d.winning_emotion, Emotion.DEAD)  # flipped by bias
        self.assertEqual(d.reply_text, "💀")

    def test_breakdown_is_explainable(self):
        comments = [Comment("💀", 10)] * 25
        d = decide_reaction(make(comments), self.p, self.s)
        self.assertIsNotNone(d.breakdown)
        self.assertIn(Emotion.DEAD, d.breakdown.final)
        self.assertEqual(d.breakdown.n_comments, 25)


if __name__ == "__main__":
    unittest.main()
