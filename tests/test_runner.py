import os
import tempfile
import unittest

from insta_reactor.backends.simulated import SimulatedBackend
from insta_reactor.config import AppConfig
from insta_reactor.models import Profile, Settings, Action, FlagKind, ReplyStyle
from insta_reactor.flags import FlagManager
from insta_reactor.runner import Runner
from insta_reactor.seen_store import SeenStore


def build_fixture():
    return {
        "chats": {
            "Best Friend": {
                "reels": [
                    {  # strong 💀 -> auto reply
                        "id": "r1", "comment_count": 240,
                        "comments": ([{"text": "LMAOO 💀💀", "likes": 90}] * 3
                                     + [{"text": "im deceased 💀", "likes": 40}] * 10
                                     + [{"text": "😭", "likes": 5}] * 5
                                     + [{"text": "🔥", "likes": 1}] * 3),
                    },
                    {  # Rule 1: text before reel
                        "id": "r2", "preceding_text": "this is literally you",
                        "comment_count": 500,
                        "comments": [{"text": "💀"}] * 50,
                    },
                ]
            },
            "Group Chat": {
                "reels": [
                    {  # Rule 2: too few comments
                        "id": "r3", "comment_count": 8,
                        "comments": [{"text": "💀"}] * 8,
                    },
                    {  # unable to read
                        "id": "r4", "read_error": True, "comments": None},
                ]
            },
            "Sarah": {
                "reels": [
                    {  # no consensus (even split)
                        "id": "r5", "comment_count": 20,
                        "comments": ([{"text": "💀"}] * 5 + [{"text": "😭"}] * 5
                                     + [{"text": "😂"}] * 5 + [{"text": "🔥"}] * 5),
                    },
                ]
            },
        }
    }


def config():
    c = AppConfig(
        profile=Profile(emoji_prefs=["💀", "😭", "😂"],
                        common_replies=["bro 💀", "nah 😭", "💀"],
                        reply_style=ReplyStyle.SINGLE),
        settings=Settings(),
        enabled_chats=["Best Friend", "Group Chat", "Sarah"],
    )
    return c


class TestRunnerEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.queue = os.path.join(self.tmp, "queue.json")
        self.seen = os.path.join(self.tmp, "seen.json")

    def _runner(self, backend, cfg=None, send=True):
        return Runner(backend, cfg or config(), FlagManager(self.queue),
                      send=send, seen_store=SeenStore(self.seen))

    def test_full_run(self):
        backend = SimulatedBackend(build_fixture())
        summary = self._runner(backend).run()

        self.assertEqual(len(summary.auto_replied), 1)
        self.assertEqual(summary.auto_replied[0].reel_id, "r1")
        # r1's most-liked comment (90 likes) is echoed back verbatim.
        self.assertEqual(summary.auto_replied[0].reply_text, "LMAOO 💀💀")
        self.assertEqual(summary.auto_replied[0].reply_source, "popular_verbatim")

        # r5 ("Sarah") is an even 4-way split, but the profile's own preferred
        # emojis (💀/😭/😂) are literally echoed 5x each in those comments, so
        # the personal-echo bypass skips the no-consensus gate — it now falls
        # through to (and fails) the confidence gate instead.
        kinds = {d.flag.kind for d in summary.flagged}
        self.assertEqual(kinds, {
            FlagKind.CONTEXT_TEXT, FlagKind.TOO_FEW_COMMENTS,
            FlagKind.UNABLE_TO_READ, FlagKind.LOW_CONFIDENCE,
        })
        # exactly one reply was actually "sent"
        self.assertEqual(len(backend.sent), 1)
        self.assertEqual(backend.sent[0], ("Best Friend", "r1", "LMAOO 💀💀"))

    def test_plan_only_sends_nothing(self):
        backend = SimulatedBackend(build_fixture())
        summary = self._runner(backend, send=False).run()
        self.assertEqual(len(summary.auto_replied), 1)
        self.assertEqual(len(backend.sent), 0)

    def test_idempotent_rerun(self):
        backend = SimulatedBackend(build_fixture())
        cfg = config()
        self._runner(backend, cfg).run()
        # second run over the same backend: r1 already reacted, nothing new sent
        summary2 = self._runner(backend, cfg).run()
        self.assertEqual(len(summary2.auto_replied), 0)
        self.assertEqual(len(backend.sent), 1)  # still just the one

    def test_seen_reel_in_middle_does_not_stop_sweep(self):
        """A previously-FLAGGED reel (in the seen store) can sit BETWEEN
        genuinely-new reels. Skipping it must NOT abandon the newer reels below
        it — the sweep continues. Regression for the over-eager `break`.
        (Note: the store now remembers only flagged reels, not reacted ones — a
        reacted reel that is re-sent must react again, see the resend test.)"""
        from insta_reactor.seen_store import signature
        from insta_reactor.models import Comment

        def laugh_reel(rid, tag):  # strong 💀-consensus -> auto-reply
            return {
                "id": rid, "comment_count": 240,
                "comments": ([{"text": f"LMAOO 💀💀 {tag}", "likes": 90}] * 3
                             + [{"text": f"im deceased 💀 {tag}", "likes": 40}] * 10
                             + [{"text": f"😭 {tag}", "likes": 5}] * 5
                             + [{"text": f"🔥 {tag}", "likes": 1}] * 3),
            }

        reel_b = laugh_reel("B", "bbb")
        fixture = {"chats": {"Feed": {"reels": [
            laugh_reel("A", "aaa"), reel_b, laugh_reel("C", "ccc"),
        ]}}}
        cfg = AppConfig(
            profile=Profile(emoji_prefs=["💀", "😭", "😂"],
                            common_replies=["bro 💀", "💀"],
                            reply_style=ReplyStyle.SINGLE),
            settings=Settings(), enabled_chats=["Feed"],
        )

        # Pre-seed the seen store with the MIDDLE reel's signature.
        b_ctx = [Comment(text=c["text"], likes=c["likes"]) for c in reel_b["comments"]]
        sig_b = signature("Feed", b_ctx)
        self.assertIsNotNone(sig_b)  # guard: the reel must actually be signable
        pre = SeenStore(self.seen)
        pre.mark(sig_b)
        pre.save()

        backend = SimulatedBackend(fixture)
        summary = self._runner(backend, cfg).run()

        replied_ids = {d.reel_id for d in summary.auto_replied}
        # A and C both reacted; B skipped as already-seen — sweep never stopped.
        self.assertEqual(replied_ids, {"A", "C"})
        self.assertEqual({s[1] for s in backend.sent}, {"A", "C"})

    def test_same_reel_yielded_twice_replies_once(self):
        """Within-run duplicate guard (regression for the live double-send):
        the real backend's newest-first enumeration can hand the SAME physical
        reel to the runner twice in one run (its scroll-based "skip" landed on
        the same reel again after the first reply mutated the thread). Both
        yields carry different reel_ids but IDENTICAL comments. The runner must
        react only ONCE, not send two different replies to the one reel."""
        def laugh_reel(rid):  # strong 💀-consensus -> auto-reply
            return {
                "id": rid, "comment_count": 240,
                "comments": ([{"text": "LMAOO 💀💀", "likes": 90}] * 3
                             + [{"text": "im deceased 💀", "likes": 40}] * 10
                             + [{"text": "😭", "likes": 5}] * 5),
            }

        # Two DIFFERENT ids, SAME content — mimics one reel enumerated twice.
        fixture = {"chats": {"Feed": {"reels": [
            laugh_reel("newreel#0"), laugh_reel("newreel#1"),
        ]}}}
        cfg = AppConfig(
            profile=Profile(emoji_prefs=["💀", "😭", "😂"],
                            common_replies=["bro 💀", "💀"],
                            reply_style=ReplyStyle.SINGLE),
            settings=Settings(), enabled_chats=["Feed"],
        )
        backend = SimulatedBackend(fixture)
        summary = self._runner(backend, cfg).run()

        self.assertEqual(len(summary.auto_replied), 1,
                         "the same reel must be replied to only once")
        self.assertEqual(len(backend.sent), 1)

    def test_reacted_reels_not_remembered_flagged_reels_are(self):
        """Dedup-policy regression (resend-reels-should-react):
        a reel we REACTED to must NOT be written to the SeenStore — otherwise a
        legitimate re-send of the same reel would be wrongly skipped. A reel we
        FLAGGED (and could sign) MUST be written, so we don't re-flag the same
        unactionable reel every run."""
        from insta_reactor.seen_store import signature
        from insta_reactor.models import Comment

        backend = SimulatedBackend(build_fixture())
        cfg = config()
        self._runner(backend, cfg).run()

        store = SeenStore(self.seen)   # reload what the run persisted

        def sig_for(chat, reel_id):
            reel = next(r for r in build_fixture()["chats"][chat]["reels"]
                        if r["id"] == reel_id)
            comments = [Comment(text=c.get("text", ""), likes=int(c.get("likes", 0)))
                        for c in (reel.get("comments") or [])]
            return signature(chat, comments)

        # r1 was auto-replied -> must NOT be remembered (so re-sends still react).
        r1_sig = sig_for("Best Friend", "r1")
        self.assertIsNotNone(r1_sig)
        self.assertFalse(store.is_reacted(r1_sig),
                         "reacted reel r1 should not be in the seen store")

        # r5 was flagged (no consensus) and is signable -> must be remembered.
        r5_sig = sig_for("Sarah", "r5")
        self.assertIsNotNone(r5_sig)
        self.assertTrue(store.is_reacted(r5_sig),
                        "flagged reel r5 should be remembered to avoid re-flagging")


if __name__ == "__main__":
    unittest.main()
