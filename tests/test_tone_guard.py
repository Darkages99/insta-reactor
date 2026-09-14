import unittest

from insta_reactor.models import Comment
from insta_reactor.engine import safety


def _c(*texts):
    return [Comment(t, 10) for t in texts]


class TestToneConflict(unittest.TestCase):
    def test_laugh_on_wholesome_is_rejected(self):
        # A reunion crowd is all warmth, zero laughter -> "lmao" clashes.
        reason = safety.tone_conflict(
            "surprised my little brother after 2 years deployed",
            _c("i'm not crying you are", "so wholesome", "🥹❤️", "beautiful"),
            "lmao")
        self.assertIsNotNone(reason)

    def test_hype_on_somber_is_rejected(self):
        reason = safety.tone_conflict(
            "saying goodbye to my best friend of 15 years. rest easy buddy 🕊️",
            _c("so sorry for your loss", "💔", "sending love"),
            "🔥")
        self.assertIsNotNone(reason)

    def test_laugh_on_educational_is_rejected(self):
        reason = safety.tone_conflict(
            "the 1400s trade route almost nobody talks about",
            _c("TIL", "so informative", "fascinating"),
            "lmao")
        self.assertIsNotNone(reason)

    def test_hype_on_educational_is_allowed(self):
        # 🔥 on an explainer reads as "cool fact", not a tone failure.
        reason = safety.tone_conflict(
            "why the sky is actually violet — a 60s explainer",
            _c("TIL", "great explanation", "never knew this"),
            "🔥")
        self.assertIsNone(reason)

    def test_laugh_on_genuinely_funny_is_allowed(self):
        # Crowd is laughing too -> guard must stay out of the way.
        reason = safety.tone_conflict(
            "he really thought he could make the jump 😹",
            _c("LMAOO", "😂😂", "im crying", "so funny"),
            "lmao")
        self.assertIsNone(reason)

    def test_hype_on_flex_is_allowed(self):
        reason = safety.tone_conflict(
            "posterized him at the buzzer 😤🏀",
            _c("BROO", "🔥🔥🔥", "cooked him", "insane hops"),
            "🔥")
        self.assertIsNone(reason)

    def test_warm_reply_never_conflicts(self):
        # A warm reaction on wholesome content is never a conflict.
        reason = safety.tone_conflict(
            "her very first steps 🥹",
            _c("the CUTEST", "🥹🥹", "precious"),
            "🥹")
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
