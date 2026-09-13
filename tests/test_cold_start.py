"""Deriving a Profile from the user's own past messages, no wizard."""

from __future__ import annotations

from insta_reactor.onboarding.cold_start import derive_profile
from insta_reactor.models import Profile, ReplyStyle


def test_emoji_prefs_ranked_by_frequency():
    msgs = ["lol 💀", "💀💀", "😭", "💀 dead", "😭 again", "💀"]
    p = derive_profile(msgs)
    # 💀 appears most, then 😭
    assert p.emoji_prefs[0] == "💀"
    assert "😭" in p.emoji_prefs


def test_infer_double_style():
    msgs = ["💀💀", "😭😭", "🔥🔥", "💀💀"]
    assert derive_profile(msgs).reply_style == ReplyStyle.DOUBLE


def test_infer_text_emoji_style():
    msgs = ["bro 💀", "nah 😭", "lmao 💀", "fr 🔥"]
    assert derive_profile(msgs).reply_style == ReplyStyle.TEXT_EMOJI


def test_infer_single_style():
    msgs = ["💀", "😭", "🔥", "💀"]
    assert derive_profile(msgs).reply_style == ReplyStyle.SINGLE


def test_common_replies_are_frequent_and_short():
    msgs = ["LMAOO", "LMAOO", "nah 😭", "nah 😭", "nah 😭",
            "this is a very long message that is real conversation not a reaction"]
    # "LMAOO" (plain text) and "nah 😭" (text+emoji) are both too contextual to
    # trust without explicit approval — only pre-approved phrases survive.
    p = derive_profile(msgs, approved_phrases=["nah 😭", "LMAOO"])
    assert "nah 😭" in p.common_replies
    assert "LMAOO" in p.common_replies
    # the long conversational line is excluded
    assert all(len(r) <= 24 for r in p.common_replies)


def test_common_replies_excludes_unapproved_contextual_text():
    msgs = ["LMAOO", "LMAOO", "nah 😭", "nah 😭", "nah 😭"]
    # No approved_phrases given -> nothing text-shaped survives, even though
    # it's frequent and short. Only bare emoji reactions would pass.
    p = derive_profile(msgs)
    assert p.common_replies == []


def test_empty_input_yields_empty_profile():
    p = derive_profile([])
    assert p.emoji_prefs == [] and p.common_replies == []
    assert p.reply_style == ReplyStyle.SINGLE


def test_existing_slang_preserved():
    existing = Profile(extra_slang={"finished": "DEAD"})
    p = derive_profile(["💀"], existing=existing)
    assert p.extra_slang == {"finished": "DEAD"}


def test_case_insensitive_reply_grouping():
    msgs = ["lmaoo", "LMAOO", "Lmaoo"]
    p = derive_profile(msgs, approved_phrases=["lmaoo"])
    # grouped into one common reply, keeping the first-seen casing; approval
    # matching is itself case-insensitive
    assert p.common_replies == ["lmaoo"]


def test_bare_emoji_reply_needs_no_approval():
    msgs = ["💀", "💀", "💀"]
    p = derive_profile(msgs)
    assert "💀" in p.common_replies
