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

        kinds = {d.flag.kind for d in summary.flagged}
        self.assertEqual(kinds, {
            FlagKind.CONTEXT_TEXT, FlagKind.TOO_FEW_COMMENTS,
            FlagKind.UNABLE_TO_READ, FlagKind.NO_CONSENSUS,
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


if __name__ == "__main__":
    unittest.main()
