"""P3 app-entry seam — the JSON boundary the V3 app (Chaquopy) calls.

These exercise `insta_reactor.app_entry` exactly as the Kotlin `EngineBridge`
will: JSON strings in, JSON strings out, a `ReplayBridge` standing in for the
live accessibility service, and a temp dir standing in for the app's files dir.
No phone, no Chaquopy — but the *real* device path (`AccessibilityDevice`) and
the *real* Runner execute, so this proves the on-device wiring end to end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `replay` importable

from replay.bridge import ReplayBridge, load_screen  # noqa: E402

from insta_reactor import app_entry  # noqa: E402
from insta_reactor.models import Profile, Settings, ReplyStyle  # noqa: E402


def test_load_config_defaults_when_absent(tmp_path):
    out = json.loads(app_entry.load_config(str(tmp_path)))
    assert out["ok"] is True
    # A fresh install has no chats and the conservative defaults.
    assert out["config"]["enabled_chats"] == []
    assert out["config"]["settings"]["min_comments"] == Settings().min_comments


def test_save_then_load_config_round_trips(tmp_path):
    cfg = {
        "profile": Profile(emoji_prefs=["💀", "😭"],
                           reply_style=ReplyStyle.DOUBLE).to_dict(),
        "settings": Settings(min_comments=20, max_new_reels=2).to_dict(),
        "enabled_chats": ["the group chat", "alex"],
    }
    saved = json.loads(app_entry.save_config(str(tmp_path), json.dumps(cfg)))
    assert saved["ok"] is True
    # The file really landed in the app's files dir.
    assert (tmp_path / "config.json").exists()

    back = json.loads(app_entry.load_config(str(tmp_path)))
    assert back["ok"] is True
    assert back["config"]["enabled_chats"] == ["the group chat", "alex"]
    assert back["config"]["profile"]["emoji_prefs"] == ["💀", "😭"]
    assert back["config"]["settings"]["max_new_reels"] == 2


def test_save_config_bad_json_returns_error_envelope(tmp_path):
    out = json.loads(app_entry.save_config(str(tmp_path), "{not json"))
    assert out["ok"] is False
    assert "error" in out  # never raises across the boundary


def test_run_dry_run_over_real_inbox_returns_summary(tmp_path):
    """A full dry run through the real AccessibilityDevice path, no phone.

    Enabled-chats is empty so the runner completes deterministically right after
    reaching the inbox — this asserts the *wiring* (config → device → backend →
    runner → JSON), not navigation. `send=False` must dispatch nothing.
    """
    # No enabled chats => the run reaches the inbox and returns an empty summary.
    app_entry.save_config(str(tmp_path), json.dumps({"enabled_chats": []}))

    bridge = ReplayBridge(load_screen("inbox"))
    out = json.loads(app_entry.run(bridge, str(tmp_path), send=False))

    assert out["ok"] is True
    assert out["send"] is False
    assert out["counts"] == {"auto_replied": 0, "flagged": 0, "by_flag": {}}
    assert out["auto_replied"] == [] and out["flagged"] == []
    # Dry run: not a single gesture was dispatched to the "device".
    assert not any(c[0] in ("tap", "swipe", "longPress", "inputText")
                   for c in bridge.calls)


def test_run_never_raises_across_boundary(tmp_path):
    """A bridge that explodes must come back as {ok:false}, not a raised exc."""
    class BrokenBridge:
        def currentPackage(self):
            raise RuntimeError("boom")
        def windowSize(self):
            return [1080, 1920]
        def nodesJson(self):
            raise RuntimeError("boom")

    app_entry.save_config(str(tmp_path), json.dumps({"enabled_chats": ["x"]}))
    out = json.loads(app_entry.run(BrokenBridge(), str(tmp_path), send=False))
    assert out["ok"] is False
    assert "error" in out


def test_review_queue_reads_persisted_items(tmp_path):
    # Seed a review queue the way a prior run would have.
    (tmp_path / "review_queue.json").write_text(json.dumps([
        {"chat_name": "alex", "reel_id": "newreel#0",
         "flag_kind": "too_few_comments", "reason": "only 4 comments",
         "confidence": 0.0, "created_at": 1.0, "resolved": False},
        {"chat_name": "alex", "reel_id": "newreel#1",
         "flag_kind": "context_text", "reason": "had preceding text",
         "confidence": 0.0, "created_at": 2.0, "resolved": True},
    ]), encoding="utf-8")

    out = json.loads(app_entry.review_queue(str(tmp_path)))
    assert out["ok"] is True
    # Only the unresolved item is pending.
    assert len(out["items"]) == 1
    assert out["items"][0]["reel_id"] == "newreel#0"
