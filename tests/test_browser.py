"""Tests for the browser backend: comment parsing, send-guard, direction, and
thumbnail plumbing. No real browser is launched — only pure logic is exercised.
"""

import os
import tempfile
import types
import unittest

from insta_reactor.browser.collector import parse_comment, parse_comments, parse_caption
from insta_reactor.browser.backend import BrowserBackend, _norm_emoji
from insta_reactor.browser.watermark import ReelWatermarkStore
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


class PositionPlumbingTests(unittest.TestCase):
    def test_position_flows_to_decision(self):
        # A reel's thread position ("Nth from the bottom") must ride the ctx all
        # the way onto the flag decision, so the review UI can tell the user
        # where to look.
        class PosBackend(SimulatedBackend):
            def build_reel_context(self, reel):
                ctx = super().build_reel_context(reel)
                ctx.position_from_bottom = 4
                return ctx

        fixtures = {"chats": {"C": {"reels": [
            {"id": "r1", "comment_count": 2,
             "comments": [{"text": "lol", "likes": 1}]}
        ]}}}
        cfg = AppConfig(profile=Profile(), settings=Settings(min_comments=15),
                        enabled_chats=["C"])
        tmp = tempfile.mkdtemp()
        runner = Runner(PosBackend(fixtures), cfg,
                        FlagManager(os.path.join(tmp, "q.json")),
                        send=False, seen_store=SeenStore(os.path.join(tmp, "s.json")),
                        log_path=os.path.join(tmp, "log.jsonl"))
        summary = runner.run()
        self.assertTrue(summary.flagged)
        self.assertEqual(summary.flagged[0].position_from_bottom, 4)

    def test_iter_reels_computes_position_from_bottom(self):
        # Fake the page's reel enumeration so iter_reels' newest-first ordering
        # and position math are exercised without a real browser. Reels are
        # ordered by viewport y (newest = largest y = nearest composer): three
        # reels at y=300,200,100 => positions 1,2,3.
        cfg = AppConfig(profile=Profile(), settings=Settings(max_new_reels=3),
                        enabled_chats=["C"])
        b = _stub_backend(cfg,
            [{"mid": f"m{i}", "y": (i + 1) * 100, "index": i, "total": 3,
              "sender": "friend", "box": {"x": 0, "y": 0, "w": 200, "h": 300}}
             for i in range(3)])
        pairs = list(b.iter_reels())
        # Newest-first by y: m2 (pos 1), m1 (pos 2), m0 (pos 3).
        ids = [ctx.reel_id for _, ctx in pairs]
        positions = [ctx.position_from_bottom for _, ctx in pairs]
        self.assertEqual(ids, ["m2", "m1", "m0"])
        self.assertEqual(positions, [1, 2, 3])


def _stub_backend(cfg, descs, watermark=None):
    """A BrowserBackend with every browser-touching helper stubbed, so
    iter_reels' pure control flow (media-id dedup, newest-first, stop-on-handled)
    can be exercised with no Playwright. _reel_descriptors returns a fixed list;
    _open_and_read_desc tags the ctx with the reel's media id."""
    from insta_reactor.models import ReelContext
    b = BrowserBackend(cfg, driver=_dummy_driver(), target_direction="any",
                       watermark_store=watermark)
    b._chat = "C"
    b._reel_descriptors = lambda: [dict(d) for d in descs]
    b._kill_screentime = lambda: 0
    b._scroll_thread_to_bottom = lambda: None
    b._settle_reels = lambda: None
    b._wheel = lambda dy: None
    b.driver.page = types.SimpleNamespace(wait_for_timeout=lambda ms: None)
    b._capture_thumbnail = lambda d, i: None
    b._open_and_read_desc = lambda d: ReelContext(
        chat_name="C", reel_id=d.get("mid") or "x")
    return b


class WatermarkDedupTests(unittest.TestCase):
    """The DM list is virtualized, so the backend identifies and dedups reels by
    their cover-image media id, remembering handled ids per chat (watermark.py).
    These exercise that against iter_reels without a real browser."""

    def _backend(self, descs):
        cfg = AppConfig(profile=Profile(), settings=Settings(max_new_reels=25),
                        enabled_chats=["C"])
        wm = ReelWatermarkStore(path=os.path.join(tempfile.mkdtemp(), "wm.json"))
        return _stub_backend(cfg, descs, watermark=wm)

    def _descs(self, mids):
        # y ordering: first mid in the list is the NEWEST (largest y).
        n = len(mids)
        return [{"mid": m, "y": (n - i) * 100, "index": i, "total": n,
                 "sender": "friend", "box": {"x": 0, "y": 0, "w": 200, "h": 300}}
                for i, m in enumerate(mids)]

    def test_all_new_reels_yielded_once_newest_first(self):
        b = self._backend(self._descs(["a", "b", "c"]))
        pairs = list(b.iter_reels())
        ids = [ctx.reel_id for _, ctx in pairs]
        self.assertEqual(ids, ["a", "b", "c"])       # newest-first, no dupes

    def test_already_handled_reels_are_skipped(self):
        b = self._backend(self._descs(["a", "b", "c"]))
        b.watermark.mark("C", "b")                   # reacted to b on a prior run
        ids = [ctx.reel_id for _, ctx in b.iter_reels()]
        self.assertNotIn("b", ids)
        self.assertIn("a", ids)
        self.assertIn("c", ids)

    def test_resend_gets_a_new_media_id_and_is_processed(self):
        # A genuine resend is a fresh bubble with a NEW media id, so it isn't in
        # the handled set and is picked up again.
        b = self._backend(self._descs(["a", "b"]))
        b.watermark.mark("C", "a")
        b.watermark.mark("C", "b")
        # resend of the same content shows up as a new bubble "b2"
        b._reel_descriptors = lambda: [dict(d) for d in self._descs(["b2", "a", "b"])]
        ids = [ctx.reel_id for _, ctx in b.iter_reels()]
        self.assertEqual(ids, ["b2"])

    def test_reel_reacted_this_run_is_not_yielded_twice(self):
        # Once send_* marks a mid handled, a re-enumeration mid-run must not
        # re-yield it (the "reacted twice" bug).
        b = self._backend(self._descs(["a", "b"]))
        seen = []
        real_open = b._open_and_read_desc
        def open_and_mark(d):
            b.watermark.mark("C", d.get("mid"))      # simulate a successful send
            return real_open(d)
        b._open_and_read_desc = open_and_mark
        for _, ctx in b.iter_reels():
            seen.append(ctx.reel_id)
        self.assertEqual(sorted(seen), ["a", "b"])
        self.assertEqual(len(seen), len(set(seen)))


class WatermarkStoreTests(unittest.TestCase):
    def test_mark_and_is_handled_roundtrip(self):
        p = os.path.join(tempfile.mkdtemp(), "wm.json")
        wm = ReelWatermarkStore(path=p)
        self.assertFalse(wm.is_handled("C", "m1"))
        wm.mark("C", "m1")
        self.assertTrue(wm.is_handled("C", "m1"))
        # persisted across instances
        self.assertTrue(ReelWatermarkStore(path=p).is_handled("C", "m1"))

    def test_none_media_id_never_handled(self):
        wm = ReelWatermarkStore(path=os.path.join(tempfile.mkdtemp(), "wm.json"))
        wm.mark("C", None)                # no-op
        self.assertFalse(wm.is_handled("C", None))

    def test_old_index_format_is_ignored_not_crashed(self):
        p = os.path.join(tempfile.mkdtemp(), "wm.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"C": {"index": 2, "total": 3}}')
        wm = ReelWatermarkStore(path=p)   # must not raise
        self.assertEqual(wm.handled_set("C"), set())


if __name__ == "__main__":
    unittest.main()
