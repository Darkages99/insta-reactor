"""End-to-end: the LLM rescue path upgrades an ambiguous flag into an auto-reply
through the real Runner, using a fake client (no network)."""

from __future__ import annotations

import os
import tempfile
import unittest

from insta_reactor.backends.simulated import SimulatedBackend
from insta_reactor.config import AppConfig
from insta_reactor.models import Profile, Settings, ReplyStyle, Action, FlagKind
from insta_reactor.flags import FlagManager
from insta_reactor.runner import Runner
from insta_reactor.seen_store import SeenStore


def even_split_fixture():
    # 4-way emoji split with no repeated profile emojis -> no_consensus flag.
    return {"chats": {"Sarah": {"reels": [{
        "id": "r5", "comment_count": 20,
        "comments": ([{"text": "🥶"}] * 5 + [{"text": "🥴"}] * 5
                     + [{"text": "🫠"}] * 5 + [{"text": "🧊"}] * 5),
    }]}}}


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user, *, temperature=0.7, max_tokens=120):
        self.calls += 1
        return self.reply


class TestLlmRescue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "log.jsonl")

    def _cfg(self, **s):
        base = dict(use_llm=True, llm_mode="assist")
        base.update(s)
        return AppConfig(
            profile=Profile(emoji_prefs=["💀"], common_replies=["💀"],
                            reply_style=ReplyStyle.SINGLE),
            settings=Settings(**base), enabled_chats=["Sarah"])

    def _run(self, cfg, client):
        backend = SimulatedBackend(even_split_fixture())
        r = Runner(backend, cfg,
                   FlagManager(os.path.join(self.tmp, "q.json")),
                   send=True, seen_store=SeenStore(os.path.join(self.tmp, "s.json")),
                   log_path=self.log)
        r.llm = client                       # inject fake (skip build_llm/key)
        return backend, r.run()

    def test_flag_upgraded_to_autoreply(self):
        client = FakeClient('{"reply":"😭😭","confidence":0.9,"should_reply":true}')
        backend, summary = self._run(self._cfg(), client)
        self.assertEqual(len(summary.auto_replied), 1)
        d = summary.auto_replied[0]
        self.assertEqual(d.reply_text, "😭😭")
        self.assertEqual(d.reply_source, "llm")
        self.assertEqual(backend.sent[0], ("Sarah", "r5", "😭😭"))
        self.assertTrue(client.calls >= 1)

    def test_low_confidence_llm_keeps_flag(self):
        client = FakeClient('{"reply":"😭","confidence":0.2,"should_reply":true}')
        backend, summary = self._run(self._cfg(), client)
        self.assertEqual(len(summary.auto_replied), 0)
        self.assertEqual(len(backend.sent), 0)
        self.assertEqual(summary.flagged[0].flag.kind, FlagKind.NO_CONSENSUS)

    def test_llm_disabled_leaves_flag(self):
        # use_llm on but we inject no client (simulate no key) -> stays flagged.
        backend = SimulatedBackend(even_split_fixture())
        cfg = self._cfg()
        r = Runner(backend, cfg, FlagManager(os.path.join(self.tmp, "q2.json")),
                   send=True, seen_store=SeenStore(os.path.join(self.tmp, "s2.json")),
                   log_path=self.log)
        r.llm = None
        summary = r.run()
        self.assertEqual(len(summary.auto_replied), 0)


if __name__ == "__main__":
    unittest.main()
