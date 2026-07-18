import os
import tempfile
import unittest

from insta_reactor.models import Comment
from insta_reactor.seen_store import signature, SeenStore


class TestSignature(unittest.TestCase):
    def test_stable_and_order_independent(self):
        a = [Comment("💀"), Comment("nah 😭"), Comment("goes hard")]
        b = list(reversed(a))
        self.assertEqual(signature("chat", a), signature("chat", b))

    def test_chat_scoped(self):
        cs = [Comment("💀"), Comment("😭"), Comment("lol")]
        self.assertNotEqual(signature("A", cs), signature("B", cs))

    def test_none_when_too_few(self):
        self.assertIsNone(signature("chat", [Comment("💀"), Comment("😭")]))

    def test_media_ignored_in_signature(self):
        base = [Comment("💀"), Comment("😭"), Comment("lol")]
        withgif = base + [Comment("GIF")]
        self.assertEqual(signature("chat", base), signature("chat", withgif))


class TestSeenStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            s = SeenStore(path)
            self.assertFalse(s.is_reacted("abc"))
            s.mark("abc")
            s.save()

            reloaded = SeenStore(path)
            self.assertTrue(reloaded.is_reacted("abc"))

    def test_none_never_reacted(self):
        with tempfile.TemporaryDirectory() as d:
            s = SeenStore(os.path.join(d, "seen.json"))
            s.mark(None)          # no-op
            self.assertFalse(s.is_reacted(None))


if __name__ == "__main__":
    unittest.main()
