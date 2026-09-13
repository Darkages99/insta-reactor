"""Tests for the browser backend: comment parsing, send-guard, direction, and
thumbnail plumbing. No real browser is launched — only pure logic is exercised.
"""

import os
import tempfile
import types
import unittest

from insta_reactor.browser.collector import parse_comment, parse_comments, parse_caption
from insta_reactor.browser.backend import BrowserBackend, _norm_emoji
from insta_reactor.backends.simulated import SimulatedBackend
from insta_reactor.config import AppConfig
from insta_reactor.models import Profile, Settings
from insta_reactor.backends.base import ReelHandle
from insta_reactor.flags import FlagManager
from insta_reactor.runner import Runner
from insta_reactor.seen_store import SeenStore


def _dummy_driver():
    # A stand-in driver so BrowserBackend can be constructed without Playwright.
    return types.SimpleNamespace(page=None)


class CommentParsingTests(unittest.TestCase):
    def test_real_comment_with_likes(self):
        li = "shiba_jayden\nIt's the garou sliding technique\n4 d24,728 likesReply\nView replies (29)"
        c = parse_comment(li)
        self.assertIsNotNone(c)
        self.assertEqual(c.text, "It's the garou sliding technique")
        self.assertEqual(c.likes, 24728)

    def test_zero_like_comment(self):
        c = parse_comment("bigadammo\nnah this is wild\n2 dReply")
        self.assertIsNotNone(c)
        self.assertEqual(c.likes, 0)
        self.assertEqual(c.text, "nah this is wild")

    def test_caption_row_skipped(self):
        # The caption has no "Reply" affordance -> not a comment.
        self.assertIsNone(parse_comment("urs_hanya\nlong caption text here\nSee Translation"))

    def test_view_replies_expander_skipped(self):
        self.assertIsNone(parse_comment("View replies (25)"))

    def test_image_only_reply_skipped(self):
        # Author then straight to the meta line => no text to react to.
        self.assertIsNone(parse_comment("aashutosshh_\n3 d23,442 likesReply\nView replies (25)"))

    def test_multiline_comment_body(self):
        c = parse_comment("user_x\nline one\nline two\n1 d5 likesReply")
        self.assertIsNotNone(c)
        self.assertEqual(c.text, "line one line two")
        self.assertEqual(c.likes, 5)

    def test_parse_comments_filters(self):
        rows = [
            "a_user\nfunny af\n1 d10 likesReply",
            "View replies (3)",
            "caption_acct\nthe caption\nSee Translation",
            "b_user\ndead 💀\n2 d0 likesReply",
        ]
        out = parse_comments(rows)
        self.assertEqual(len(out), 2)
        self.assertEqual([c.text for c in out], ["funny af", "dead 💀"])


class CaptionParsingTests(unittest.TestCase):
    def test_strips_handle_and_trailing_age(self):
        cap = parse_caption("learnwithme\n3 apps to learn coding fast\n2 d")
        self.assertEqual(cap, "3 apps to learn coding fast")

    def test_multiline_caption_joined(self):
        cap = parse_caption("acct\nline one\nline two\n5 hours ago\nmore")
        self.assertEqual(cap, "line one line two")

    def test_drops_counts_and_translation(self):
        cap = parse_caption("acct\nreal caption here\n1,234 likes\nSee translation")
        self.assertEqual(cap, "real caption here")

    def test_empty_and_handle_only(self):
        self.assertEqual(parse_caption(""), "")
        self.assertEqual(parse_caption("just_a_handle"), "")

    def test_truncates_to_max_len(self):
        cap = parse_caption("acct\n" + "x" * 900, max_len=100)
        self.assertEqual(len(cap), 100)


class SendGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig(profile=Profile(), settings=Settings())

    def test_whitelist_blocks_other_chats(self):
        b = BrowserBackend(self.cfg, driver=_dummy_driver(),
                           send_whitelist={"Sarang D Rajgopaul"})
        b._chat = "Sarang D Rajgopaul"
        self.assertTrue(b._send_allowed())
        b._chat = "Someone Else"
        self.assertFalse(b._send_allowed())

    def test_no_whitelist_allows_all(self):
        b = BrowserBackend(self.cfg, driver=_dummy_driver(), send_whitelist=None)
        b._chat = "anyone"
        self.assertTrue(b._send_allowed())

    def test_send_reply_refuses_when_blocked(self):
        b = BrowserBackend(self.cfg, driver=_dummy_driver(),
                           send_whitelist={"allowed"})
        b._chat = "blocked"
        # Must return False WITHOUT touching the (None) page.
        self.assertFalse(b.send_reply(ReelHandle(reel_id="r", locator={"index": 0}), "hi"))
        self.assertFalse(b.send_reaction(ReelHandle(reel_id="r", locator={"index": 0}), "❤️"))


class DirectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig(profile=Profile(), settings=Settings())

    def test_incoming_excludes_own(self):
        b = BrowserBackend(self.cfg, driver=_dummy_driver(),
                           target_direction="incoming", self_username="me")
        self.assertFalse(b._is_target({"sender": "me"}))
        self.assertTrue(b._is_target({"sender": "friend"}))
        self.assertTrue(b._is_target({"sender": ""}))   # unknown -> treat incoming

    def test_any_targets_everything(self):
        b = BrowserBackend(self.cfg, driver=_dummy_driver(),
                           target_direction="any", self_username="me")
        self.assertTrue(b._is_target({"sender": "me"}))


class EmojiNormTests(unittest.TestCase):
    def test_variation_selector_stripped(self):
        self.assertEqual(_norm_emoji("❤️"), _norm_emoji("❤"))


class ThumbnailPlumbingTests(unittest.TestCase):
    def test_thumbnail_flows_to_decision(self):
        # A backend that attaches a thumbnail to every reel context. The reel has
        # too few comments, so it flags — and the flag decision must carry the
        # thumbnail so the UI gallery can show it.
        class ThumbBackend(SimulatedBackend):
            def build_reel_context(self, reel):
                ctx = super().build_reel_context(reel)
                ctx.thumbnail_path = "data/thumbs/demo.png"
                return ctx

        fixtures = {"chats": {"C": {"reels": [
            {"id": "r1", "comment_count": 2,
             "comments": [{"text": "lol", "likes": 1}, {"text": "haha", "likes": 0}]}
        ]}}}
        cfg = AppConfig(profile=Profile(), settings=Settings(min_comments=15),
                        enabled_chats=["C"])
        tmp = tempfile.mkdtemp()
        runner = Runner(ThumbBackend(fixtures), cfg,
                        FlagManager(os.path.join(tmp, "q.json")),
                        send=False, seen_store=SeenStore(os.path.join(tmp, "s.json")),
                        log_path=os.path.join(tmp, "log.jsonl"))
        summary = runner.run()
        self.assertTrue(summary.flagged)
        self.assertEqual(summary.flagged[0].thumbnail_path, "data/thumbs/demo.png")


if __name__ == "__main__":
    unittest.main()
