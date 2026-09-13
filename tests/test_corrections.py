"""Correction learning: folding overrides into the profile + adapting thresholds."""

from __future__ import annotations

from insta_reactor.models import Profile, Settings, ReplyStyle
from insta_reactor.learning.corrections import (
    learn_profile, agreement_rate, adapt_settings,
)


def _ov(user_reply, bot_reply="😭"):
    return {"kind": "override", "user_reply": user_reply, "bot_reply": bot_reply}


def test_learn_promotes_corrected_emoji():
    # profile prefers 😭 first; user keeps correcting toward 💀
    p = Profile(emoji_prefs=["😭", "💀"], common_replies=[], reply_style=ReplyStyle.SINGLE)
    overrides = [_ov("💀"), _ov("💀💀"), _ov("💀")]
    out = learn_profile(p, overrides)
    assert out.emoji_prefs[0] == "💀"          # corrected emoji promoted to front


def test_learn_adds_repeated_reply():
    p = Profile(emoji_prefs=["💀"], common_replies=["💀"], reply_style=ReplyStyle.TEXT_EMOJI)
    overrides = [_ov("nah 😭"), _ov("nah 😭")]   # repeated -> added
    out = learn_profile(p, overrides, approved_phrases=["nah 😭"])
    assert "nah 😭" in out.common_replies


def test_learn_drops_repeated_reply_without_approval():
    # Same repeated correction, but nothing on the approved list -> too
    # contextual to trust even though the user corrected toward it repeatedly.
    p = Profile(emoji_prefs=["💀"], common_replies=["💀"], reply_style=ReplyStyle.TEXT_EMOJI)
    overrides = [_ov("nah 😭"), _ov("nah 😭")]
    out = learn_profile(p, overrides)
    assert "nah 😭" not in out.common_replies


def test_learn_ignores_one_off_reply():
    p = Profile(emoji_prefs=["💀"], common_replies=[], reply_style=ReplyStyle.SINGLE)
    out = learn_profile(p, [_ov("some one-off thing 😭")])
    assert "some one-off thing 😭" not in out.common_replies


def test_learn_switches_style_when_dominant():
    p = Profile(emoji_prefs=["💀"], common_replies=[], reply_style=ReplyStyle.SINGLE)
    overrides = [_ov("bro 💀"), _ov("nah 😭"), _ov("lmao 💀"),
                 _ov("fr 🔥"), _ov("dead 💀")]
    assert learn_profile(p, overrides).reply_style == ReplyStyle.TEXT_EMOJI


def test_learn_noop_without_overrides():
    p = Profile(emoji_prefs=["💀"], common_replies=["💀"])
    assert learn_profile(p, []) is p


def test_input_profile_not_mutated():
    p = Profile(emoji_prefs=["😭"], common_replies=[])
    learn_profile(p, [_ov("💀"), _ov("💀")])
    assert p.emoji_prefs == ["😭"]               # original untouched


def test_agreement_rate():
    recs = (
        [{"kind": "decision", "action": "auto_reply"}] * 8
        + [{"kind": "decision", "action": "flag"}] * 2
        + [{"kind": "override", "bot_reply": "😭", "user_reply": "💀"}] * 2
    )
    # 8 proposals, 2 overridden => 75% agreement
    assert agreement_rate(recs) == 0.75


def test_agreement_none_without_proposals():
    assert agreement_rate([{"kind": "decision", "action": "flag"}]) is None


def test_adapt_lowers_bar_on_high_agreement():
    s = Settings(auto_reply_min_confidence=0.6)
    out = adapt_settings(s, agreement=0.95, n_samples=100)
    assert out.auto_reply_min_confidence < 0.6   # trust automation more


def test_adapt_raises_bar_on_low_agreement():
    s = Settings(auto_reply_min_confidence=0.6)
    out = adapt_settings(s, agreement=0.60, n_samples=100)
    assert out.auto_reply_min_confidence > 0.6   # be more cautious


def test_adapt_needs_enough_samples():
    s = Settings(auto_reply_min_confidence=0.6)
    assert adapt_settings(s, agreement=0.95, n_samples=5) is s   # too few -> unchanged


def test_adapt_bounded():
    s = Settings(auto_reply_min_confidence=0.9)
    out = adapt_settings(s, agreement=0.95, n_samples=100)
    assert 0.50 <= out.auto_reply_min_confidence <= 0.75
