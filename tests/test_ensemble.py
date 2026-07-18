import unittest

from insta_reactor.models import Comment, Settings, Emotion
from insta_reactor.engine import scoring
from insta_reactor.engine.classifier import build_model, GOEMOTION_TO_BUCKET


class FakeModel:
    """Returns a fixed per-comment distribution, ignoring the text."""
    def __init__(self, dist):
        self.dist = dist

    def classify_batch(self, texts):
        return [dict(self.dist) for _ in texts]


class TestEnsemble(unittest.TestCase):
    def test_no_model_is_rules_only(self):
        s = Settings(like_weight_mode="none")
        comments = [Comment("💀"), Comment("idk man")]
        self.assertEqual(scoring.public_scores(comments, s),
                         scoring.public_scores(comments, s, model=None))

    def test_model_recovers_signal_rules_miss(self):
        # "idk man" has no rule signal; the model supplies LAUGH.
        s = Settings(like_weight_mode="none", ensemble_model_weight=0.5)
        comments = [Comment("idk man")]
        raw = scoring.public_scores(comments, s, model=FakeModel({Emotion.LAUGH: 1.0}))
        self.assertGreater(raw.get(Emotion.LAUGH, 0), 0)

    def test_rules_keep_meme_bucket_at_default_weight(self):
        # Comment is clearly 💀 (DEAD) to the rules; model says LAUGH. At the
        # default weight the rules keep the edge (model has no DEAD class).
        s = Settings(like_weight_mode="none")
        self.assertLess(s.ensemble_model_weight, 0.5)
        comments = [Comment("im deceased 💀")]
        raw = scoring.public_scores(comments, s, model=FakeModel({Emotion.LAUGH: 1.0}))
        winner = max(raw, key=raw.get)
        self.assertEqual(winner, Emotion.DEAD)

    def test_high_model_weight_can_override(self):
        s = Settings(like_weight_mode="none", ensemble_model_weight=0.9)
        comments = [Comment("im deceased 💀")]
        raw = scoring.public_scores(comments, s, model=FakeModel({Emotion.CRYING: 1.0}))
        self.assertEqual(max(raw, key=raw.get), Emotion.CRYING)

    def test_build_model_disabled_by_default(self):
        self.assertIsNone(build_model(Settings()))
        self.assertIsNone(build_model(Settings(use_model=True,
                                               ensemble_model_weight=0.0)))

    def test_mapping_has_no_dead(self):
        self.assertNotIn(Emotion.DEAD, set(GOEMOTION_TO_BUCKET.values()))
        self.assertIn(Emotion.LAUGH, set(GOEMOTION_TO_BUCKET.values()))


if __name__ == "__main__":
    unittest.main()
