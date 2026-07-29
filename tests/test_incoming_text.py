"""Incoming-text detection + push notifications.

Covers the two things the user asked to be alerted about:
  1. ANY incoming text message in a chat (even one) is flagged + pushed.
  2. Anything the bot couldn't respond to (nav/send/read failure) is pushed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `replay` importable

from replay.bridge import ReplayBridge  # noqa: E402

from insta_reactor import notify
from insta_reactor.backends.android import AndroidBackend
from insta_reactor.backends.base import Backend, ReelHandle
from insta_reactor.config import AppConfig
from insta_reactor.device.accessibility_device import AccessibilityDevice
from insta_reactor.models import (
    Action, Decision, Flag, FlagKind, Profile, RunSummary, Settings,
)
from insta_reactor.runner import Runner
from insta_reactor.flags import FlagManager
from insta_reactor.seen_store import SeenStore


# --------------------------------------------------------------------------
# notify.notify_from_summary
# --------------------------------------------------------------------------

def _capture(monkeypatch):
    sent: list[tuple] = []

    def fake_push(topic, title, message, priority="high"):
        sent.append((topic, title, message, priority))
        return True

    monkeypatch.setattr(notify, "push", fake_push)
    return sent


def _flag(kind, reason, chat="freakhan"):
    return Decision(action=Action.FLAG, flag=Flag(kind, reason), chat_name=chat)


class TestNotifyFromSummary:
    def test_single_text_pushes_high_priority(self, monkeypatch):
        sent = _capture(monkeypatch)
        s = RunSummary()
        s.add(_flag(FlagKind.INCOMING_TEXT, "yo you up?"))
        notify.notify_from_summary("topic", s)
        assert len(sent) == 1
        _, title, body, prio = sent[0]
        assert "freakhan" in title and "New text" in title
        assert prio == "high"
        assert "yo you up?" in body

    def test_texts_grouped_per_chat(self, monkeypatch):
        sent = _capture(monkeypatch)
        s = RunSummary()
        s.add(_flag(FlagKind.INCOMING_TEXT, "one", chat="freakhan"))
        s.add(_flag(FlagKind.INCOMING_TEXT, "two", chat="freakhan"))
        s.add(_flag(FlagKind.INCOMING_TEXT, "hi", chat="BABAYAGA-Anirudh"))
        notify.notify_from_summary("topic", s)
        # one push per chat
        assert len(sent) == 2
        titles = " ".join(t for _, t, _, _ in sent)
        assert "freakhan" in titles and "BABAYAGA-Anirudh" in titles
        freak = next(b for _, t, b, _ in sent if "freakhan" in t)
        assert "one" in freak and "two" in freak

    def test_couldnt_respond_pushes_high_priority(self, monkeypatch):
        sent = _capture(monkeypatch)
        s = RunSummary()
        s.add(_flag(FlagKind.NAV_FAILED, "Reply send failed; queued."))
        s.add(_flag(FlagKind.UNABLE_TO_READ, "could not read comments"))
        notify.notify_from_summary("topic", s)
        assert len(sent) == 1
        _, title, _, prio = sent[0]
        assert "Couldn't respond" in title and "2" in title
        assert prio == "high"

    def test_clean_run_sends_nothing(self, monkeypatch):
        sent = _capture(monkeypatch)
        s = RunSummary()
        s.add(Decision(action=Action.AUTO_REPLY, reply_text="😂",
                       chat_name="freakhan"))
        notify.notify_from_summary("topic", s)
        assert sent == []

    def test_empty_topic_skips_without_error(self):
        s = RunSummary()
        s.add(_flag(FlagKind.INCOMING_TEXT, "hi"))
        # real push() with empty topic must no-op, returning 0 sent
        assert notify.notify_from_summary("", s) == 0


# --------------------------------------------------------------------------
# Runner flags incoming text (even one)
# --------------------------------------------------------------------------

class _TextOnlyBackend(Backend):
    """A backend with no reels but one incoming text message."""

    def __init__(self, texts):
        self._texts = texts

    def prepare(self): pass
    def open_chat(self, chat_name): return True
    def find_unreacted_reels(self): return []
    def build_reel_context(self, reel): raise NotImplementedError
    def send_reply(self, reel, text): return True
    def return_to_inbox(self): pass
    def iter_reels(self): return iter(())
    def unanswered_incoming_texts(self, cap=8): return list(self._texts)


def _config():
    return AppConfig(profile=Profile(emoji_prefs=["😂"]),
                     settings=Settings(), enabled_chats=["freakhan"])


def test_runner_flags_single_incoming_text(tmp_path):
    backend = _TextOnlyBackend(["yo you up?"])
    runner = Runner(backend, _config(),
                    FlagManager(str(tmp_path / "q.json")),
                    seen_store=SeenStore(str(tmp_path / "s.json")))
    summary = runner.run()
    texts = [d for d in summary.flagged
             if d.flag and d.flag.kind == FlagKind.INCOMING_TEXT]
    assert len(texts) == 1
    assert texts[0].flag.reason == "yo you up?"
    assert texts[0].chat_name == "freakhan"


def test_runner_no_text_no_flag(tmp_path):
    backend = _TextOnlyBackend([])
    runner = Runner(backend, _config(),
                    FlagManager(str(tmp_path / "q.json")),
                    seen_store=SeenStore(str(tmp_path / "s.json")))
    summary = runner.run()
    assert summary.flagged == []


# --------------------------------------------------------------------------
# AndroidBackend.unanswered_incoming_texts (watermark logic, real filtering)
# --------------------------------------------------------------------------

def _n(cls, text="", id="", bounds=(0, 0, 0, 0)):
    return {"cls": cls, "text": text, "desc": "", "id": id, "bounds": list(bounds),
            "clickable": False, "scrollable": False, "longClickable": False,
            "enabled": True}


def _android(nodes, monkeypatch):
    dev = AccessibilityDevice(ReplayBridge(nodes))  # default size 1272x2772
    be = AndroidBackend(AppConfig(), device=dev)
    be._chat = "freakhan"
    monkeypatch.setattr(be, "_anchor_chat_bottom", lambda: True)
    monkeypatch.setattr(be.nav, "thread_scroll_up", lambda amount=0.3: False)
    return be


TV = "android.widget.TextView"


class TestUnansweredIncomingTexts:
    def test_reports_only_text_newer_than_last_reply(self, monkeypatch):
        # window 1272x2772 -> band ~ (443, 2273); midline x=636
        nodes = [
            _n(TV, "old message", bounds=(150, 1000, 500, 1060)),   # older, ignore
            _n(TV, "😂", bounds=(900, 1500, 1100, 1560)),           # our reply (wm)
            _n(TV, "yo you up?", bounds=(150, 1800, 500, 1860)),    # NEW incoming
            _n(TV, "some.handle", id="com.instagram.android:id/title_text",
               bounds=(150, 1700, 400, 1740)),                       # attribution
            _n(TV, "Tap and hold to react",
               id="com.instagram.android:id/message_footer_label",
               bounds=(150, 1750, 500, 1790)),                       # UI chrome
        ]
        be = _android(nodes, monkeypatch)
        assert be.unanswered_incoming_texts() == ["yo you up?"]

    def test_idempotent_when_reply_is_newest(self, monkeypatch):
        nodes = [
            _n(TV, "yo you up?", bounds=(150, 1500, 500, 1560)),   # incoming
            _n(TV, "😂", bounds=(900, 1800, 1100, 1860)),         # our reply below it
        ]
        be = _android(nodes, monkeypatch)
        assert be.unanswered_incoming_texts() == []

    def test_never_replied_reports_recent_texts(self, monkeypatch):
        nodes = [
            _n(TV, "first", bounds=(150, 1000, 500, 1060)),
            _n(TV, "second", bounds=(150, 1400, 500, 1460)),
        ]
        be = _android(nodes, monkeypatch)
        assert be.unanswered_incoming_texts() == ["first", "second"]

    def test_reel_caption_inside_bubble_is_excluded(self, monkeypatch):
        """A reel's own caption renders as a TextView INSIDE the shared-reel
        bubble; it must not be reported as a chat message (regression from the
        first live run on freakhan). A real message beside the reel still is."""
        reel = {"cls": "android.widget.FrameLayout", "text": "", "desc": "",
                "id": "com.instagram.android:id/reel_share_item_view",
                "bounds": [158, 1535, 636, 2395], "clickable": True,
                "scrollable": False, "longClickable": False, "enabled": True}
        nodes = [
            reel,
            _n(TV, "Hi von", bounds=(150, 1000, 400, 1060)),                      # real msg above reel
            _n(TV, "zuckubus long caption text", bounds=(180, 1800, 620, 2000)),  # inside reel
        ]
        be = _android(nodes, monkeypatch)
        assert be.unanswered_incoming_texts() == ["Hi von"]

    def test_long_caption_block_skipped_and_snippet_truncated(self, monkeypatch):
        long_caption = "zuckubus " + ("blah " * 100)   # >220 chars => a caption
        medium = "x" * 200                              # real-ish, gets truncated
        nodes = [
            _n(TV, long_caption, bounds=(150, 900, 620, 1400)),
            _n(TV, medium, bounds=(150, 1500, 620, 1700)),
        ]
        be = _android(nodes, monkeypatch)
        out = be.unanswered_incoming_texts()
        assert long_caption not in out            # caption dropped entirely
        assert len(out) == 1 and out[0].endswith("…")   # medium kept, truncated

    def test_no_watermark_does_not_history_dive(self, monkeypatch):
        """Without an outgoing watermark we report only the current screenful
        and never scroll into history (avoids flooding a never-replied thread)."""
        nodes = [_n(TV, "recent", bounds=(150, 1500, 500, 1560))]
        be = _android(nodes, monkeypatch)
        scrolls = {"n": 0}

        def counting_scroll(amount=0.3):
            scrolls["n"] += 1
            return True

        monkeypatch.setattr(be.nav, "thread_scroll_up", counting_scroll)
        assert be.unanswered_incoming_texts() == ["recent"]
        assert scrolls["n"] == 0   # no history-dive without a watermark
