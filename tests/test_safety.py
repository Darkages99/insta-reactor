"""The deterministic content-safety gate: never auto-react to cruel/bigoted reels."""

import unittest

from insta_reactor.engine.safety import harmful_reason
from insta_reactor.engine.reaction import decide_reaction
from insta_reactor.models import (
    Comment, ReelContext, Profile, Settings, Action, FlagKind,
)


def _reel(caption, comment_texts):
    return ReelContext(
        chat_name="c", reel_id="r1", caption=caption,
        comments=[Comment(t, 20) for t in comment_texts],
        comment_count=len(comment_texts))


class HarmfulReasonTests(unittest.TestCase):
    def test_flags_bullying(self):
        self.assertIsNotNone(harmful_reason(
            "we made her cry again 😂 what a loser", [Comment("get rekt", 5)]))

    def test_flags_bigotry(self):
        self.assertIsNotNone(harmful_reason(
            "ranking entire groups of people worst to best", []))

    def test_flags_self_harm(self):
        self.assertIsNotNone(harmful_reason("just kys honestly", []))

    def test_flags_from_comments_too(self):
        self.assertIsNotNone(harmful_reason(
            "clip", [Comment("nobody likes you", 3), Comment("lol", 1)]))

    def test_ignores_sports_and_hype_slang(self):
        # "cooked him", "goes crazy", "insane" are normal hype — never flagged.
        for cap in ("he cooked him at the buzzer", "this goes crazy fr",
                    "insane trick shot", "posterized him"):
            self.assertIsNone(harmful_reason(cap, [Comment("🔥", 9)]), cap)

    def test_ignores_ordinary_wholesome(self):
        self.assertIsNone(harmful_reason(
            "her first steps 🥹", [Comment("so cute", 9)]))


class SafetyGateTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(emoji_prefs=["🔥"], common_replies=["lmao"])
        self.settings = Settings(use_llm=False, min_comments=3)

    def test_decide_flags_sensitive_content(self):
        ctx = _reel("we made her cry again, what a loser",
                    ["lmao", "get rekt", "she deserved it", "😂"])
        d = decide_reaction(ctx, self.profile, self.settings)
        self.assertEqual(d.action, Action.FLAG)
        self.assertEqual(d.flag.kind, FlagKind.SENSITIVE_CONTENT)

    def test_decide_does_not_flag_normal_reel(self):
        ctx = _reel("insane dunk", ["🔥", "goes crazy", "cooked him", "W"])
        d = decide_reaction(ctx, self.profile, self.settings)
        self.assertNotEqual(
            getattr(d.flag, "kind", None), FlagKind.SENSITIVE_CONTENT)


if __name__ == "__main__":
    unittest.main()
