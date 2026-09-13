"""Eval harness: behavioural metrics + replay against the current engine."""

from __future__ import annotations

from insta_reactor.tools.eval_harness import evaluate
from insta_reactor.models import Profile, Settings, ReplyStyle


def _decision(action, *, reply=None, source=None, flag=None, sent=False,
              sig="", comments_raw=None):
    return {"kind": "decision", "action": action, "reply_text": reply,
            "reply_source": source, "flag_kind": flag, "sent": sent,
            "comment_sig": sig, "comments_raw": comments_raw}


def test_behavioural_counts():
    recs = [
        _decision("auto_reply", reply="💀", source="emotion", sent=True),
        _decision("auto_reply", reply="😭", source="popular_verbatim", sent=True),
        _decision("flag", flag="no_consensus"),
        _decision("flag", flag="low_confidence"),
        {"kind": "override", "bot_reply": "💀", "user_reply": "😭", "comment_sig": "x"},
    ]
    rep = evaluate(recs)
    assert rep.n_decisions == 4
    assert rep.n_auto == 2 and rep.n_flag == 2 and rep.n_sent == 2
    assert rep.n_overrides == 1
    assert rep.flag_counts == {"no_consensus": 1, "low_confidence": 1}
    assert rep.source_counts == {"emotion": 1, "popular_verbatim": 1}
    # 2 proposals, 1 override => 50%
    assert rep.agreement == 0.5


def test_replay_reproduces_logged_reply():
    # a strong 💀-consensus reel; the engine should still echo the popular comment
    raw = ([{"text": "LMAOO 💀💀", "likes": 90}] * 3
           + [{"text": "im deceased 💀", "likes": 40}] * 10
           + [{"text": "😭", "likes": 5}] * 5)
    rec = _decision("auto_reply", reply="LMAOO 💀💀", source="popular_verbatim",
                    sent=True, sig="s1", comments_raw=raw)
    profile = Profile(emoji_prefs=["💀", "😭"], common_replies=["💀"],
                      reply_style=ReplyStyle.SINGLE)
    rep = evaluate([rec], profile, Settings())
    assert rep.n_replayable == 1
    assert rep.replay_reproduced == 1


def test_replay_skipped_without_raw_comments():
    rec = _decision("auto_reply", reply="💀", source="emotion", sent=True)
    rep = evaluate([rec], Profile(), Settings())
    assert rep.n_replayable == 0


def test_replay_matches_override():
    raw = ([{"text": "LMAOO 💀💀", "likes": 90}] * 3
           + [{"text": "im deceased 💀", "likes": 40}] * 10
           + [{"text": "😭", "likes": 5}] * 5)
    sig = "s2"
    rec = _decision("auto_reply", reply="LMAOO 💀💀", source="popular_verbatim",
                    sent=True, sig=sig, comments_raw=raw)
    # you corrected it to exactly the same verbatim echo the engine produces
    override = {"kind": "override", "comment_sig": sig,
                "bot_reply": "😭", "user_reply": "LMAOO 💀💀"}
    profile = Profile(emoji_prefs=["💀"], common_replies=["💀"],
                      reply_style=ReplyStyle.SINGLE)
    rep = evaluate([rec, override], profile, Settings())
    assert rep.replay_matches_override == 1


def test_empty_log():
    rep = evaluate([])
    assert rep.n_decisions == 0 and rep.agreement is None
    assert "decisions logged : 0" in rep.summary()
