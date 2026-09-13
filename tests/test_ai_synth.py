"""The AI-native reply path: the LLM synthesises/rescues the reaction.

Uses a fake ChatClient so nothing hits the network; asserts the runner's
_maybe_llm both SYNTHESISES (llm_mode='always') on an auto-reply and RESCUES an
ambiguous flag, and is a strict no-op when the model is off.
"""

import json
import unittest

from insta_reactor.config import AppConfig
from insta_reactor.models import (
    Profile, Settings, ReelContext, Comment, Decision, Action, Flag, FlagKind,
)
from insta_reactor.runner import Runner
from insta_reactor.rag.store import RagStore
from insta_reactor.backends.simulated import SimulatedBackend


class FakeLLM:
    def __init__(self, reply, confidence=0.9, should_reply=True):
        self.reply, self.confidence, self.should_reply = reply, confidence, should_reply
        self.calls = []

    def complete(self, system, user, *, temperature=0.7, max_tokens=120):
        self.calls.append((system, user))
        return json.dumps({"reply": self.reply, "confidence": self.confidence,
                           "should_reply": self.should_reply})


def _ctx():
    return ReelContext(chat_name="c", reel_id="sc123", comments=[
        Comment("lmaooo 💀💀", 12), Comment("dead 😭", 8),
        Comment("this is so funny", 3), Comment("goes hard", 2)])


def _runner(mode="always", reply="bro 💀"):
    cfg = AppConfig(profile=Profile(emoji_prefs=["💀", "😭"]),
                    settings=Settings(use_llm=True, llm_mode=mode))
    r = Runner(SimulatedBackend({"chats": {}}), cfg, send=False)
    r.llm = FakeLLM(reply)          # inject deterministic fake
    r.rag = RagStore()              # empty RAG, no disk/network
    return r


class SynthesiseTests(unittest.TestCase):
    def test_always_mode_synthesises_reply(self):
        r = _runner(mode="always", reply="bro 💀")
        dec = Decision(action=Action.AUTO_REPLY, reply_text="💀",
                       winning_emotion="DEAD", confidence=0.7)
        out = r._maybe_llm(_ctx(), dec)
        self.assertEqual(out.action, Action.AUTO_REPLY)
        self.assertEqual(out.reply_text, "bro 💀")
        self.assertEqual(out.reply_source, "llm")

    def test_grounding_is_passed_to_model(self):
        r = _runner(mode="always")
        dec = Decision(action=Action.AUTO_REPLY, reply_text="💀",
                       winning_emotion="DEAD", confidence=0.7)
        r._maybe_llm(_ctx(), dec)
        _, user_msg = r.llm.calls[0]
        self.assertIn("DEAD", user_msg)          # the crowd-analysis grounding
        self.assertIn("CROWD'S COMMENTS", user_msg)

    def test_assist_mode_does_not_synthesise_confident_reply(self):
        # In 'assist' mode a confident auto-reply is left untouched.
        r = _runner(mode="assist")
        dec = Decision(action=Action.AUTO_REPLY, reply_text="💀",
                       winning_emotion="DEAD", confidence=0.7)
        out = r._maybe_llm(_ctx(), dec)
        self.assertEqual(out.reply_text, "💀")
        self.assertNotEqual(out.reply_source, "llm")


class RescueTests(unittest.TestCase):
    def test_low_confidence_flag_is_rescued(self):
        r = _runner(mode="assist", reply="😭")
        dec = Decision(action=Action.FLAG,
                       flag=Flag(FlagKind.LOW_CONFIDENCE, "unsure"),
                       winning_emotion="CRYING", confidence=0.3)
        out = r._maybe_llm(_ctx(), dec)
        self.assertEqual(out.action, Action.AUTO_REPLY)
        self.assertEqual(out.reply_text, "😭")
        self.assertEqual(out.reply_source, "llm")

    def test_context_text_flag_not_rescued(self):
        # A reel with preceding text needs a human — never AI'd.
        r = _runner(mode="always", reply="😭")
        dec = Decision(action=Action.FLAG,
                       flag=Flag(FlagKind.CONTEXT_TEXT, "inside joke"),
                       confidence=0.0)
        out = r._maybe_llm(_ctx(), dec)
        self.assertEqual(out.action, Action.FLAG)

    def test_noop_when_llm_disabled(self):
        r = _runner(mode="always")
        r.llm = None
        dec = Decision(action=Action.AUTO_REPLY, reply_text="💀", confidence=0.7)
        out = r._maybe_llm(_ctx(), dec)
        self.assertIs(out, dec)


if __name__ == "__main__":
    unittest.main()
