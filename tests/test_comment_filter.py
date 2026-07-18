import unittest

from insta_reactor.models import Comment
from insta_reactor.engine.comment_filter import is_reactable_comment, reactable


class TestCommentFilter(unittest.TestCase):
    def test_text_and_emoji_kept(self):
        self.assertTrue(is_reactable_comment("LMAOO 💀"))
        self.assertTrue(is_reactable_comment("😭"))
        self.assertTrue(is_reactable_comment("this goes hard"))

    def test_media_placeholders_dropped(self):
        for t in ("GIF", "gif", "Sticker", "Photo", "video", "sent an attachment"):
            self.assertFalse(is_reactable_comment(t), t)

    def test_empty_dropped(self):
        self.assertFalse(is_reactable_comment(""))
        self.assertFalse(is_reactable_comment("   "))
        self.assertFalse(is_reactable_comment(None))

    def test_artifacts_dropped(self):
        self.assertFalse(is_reactable_comment("Reply"))
        self.assertFalse(is_reactable_comment("likes"))

    def test_word_mentioning_gif_kept(self):
        # only a *bare* placeholder is dropped; a real sentence survives
        self.assertTrue(is_reactable_comment("that gif killed me 💀"))

    def test_reactable_filters_list(self):
        cs = [Comment("💀"), Comment("GIF"), Comment(""), Comment("nah 😭")]
        kept = reactable(cs)
        self.assertEqual([c.text for c in kept], ["💀", "nah 😭"])


if __name__ == "__main__":
    unittest.main()
