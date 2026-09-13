"""Interaction log + RAG retrieval, exercised on a temp log file."""

from __future__ import annotations

import os

from insta_reactor import interaction_log as il
from insta_reactor.models import (
    Comment, ReelContext, Decision, Action, Flag, FlagKind,
)
from insta_reactor.rag.store import RagStore


def _ctx(texts, chat="c1", reel="r1"):
    return ReelContext(chat_name=chat, reel_id=reel,
                       comments=[Comment(t) for t in texts])


def _auto(reply, chat="c1", reel="r1", emotion="CRYING"):
    return Decision(action=Action.AUTO_REPLY, reply_text=reply,
                    reply_source="emotion", winning_emotion=emotion,
                    confidence=0.8, chat_name=chat, reel_id=reel)


def test_log_and_read_roundtrip(tmp_path):
    path = str(tmp_path / "log.jsonl")
    ctx = _ctx(["lol 😭", "dead 😭😭", "cryinggg 😭"])
    il.log_decision(ctx, _auto("😭😭"), sent=True, run_id="run1", path=path)
    recs = il.read_kind("decision", path=path)
    assert len(recs) == 1
    r = recs[0]
    assert r["reply_text"] == "😭😭"
    assert r["sent"] is True
    assert r["emoji_hist"].get("😭") == 4      # histogram across comments
    # raw comment text is NOT stored by default
    assert "comments_raw" not in r


def test_raw_text_opt_in(tmp_path):
    path = str(tmp_path / "log.jsonl")
    ctx = _ctx(["secret joke here 😭", "aaa 😭", "bbb 😭"])
    il.log_decision(ctx, _auto("😭"), sent=True, path=path, log_raw_text=True)
    r = il.read_kind("decision", path=path)[0]
    assert r["comments_raw"][0]["text"] == "secret joke here 😭"


def test_corrupt_lines_skipped(tmp_path):
    path = str(tmp_path / "log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"kind":"decision","reply_text":"ok"}\n')
        f.write("not json at all\n")
        f.write('{"kind":"override","user_reply":"💀"}\n')
    assert len(list(il.read_all(path))) == 2


def test_override_logged(tmp_path):
    path = str(tmp_path / "log.jsonl")
    il.log_override(chat="c1", reel_id="r1", comment_sig="abc",
                    bot_reply="😭", user_reply="💀💀", path=path)
    r = il.read_kind("override", path=path)[0]
    assert r["user_reply"] == "💀💀" and r["bot_reply"] == "😭"


def test_rag_retrieves_similar_by_emoji(tmp_path):
    path = str(tmp_path / "log.jsonl")
    # two very different past reels
    il.log_decision(_ctx(["😭 sad", "😭😭", "cry 😭"], reel="a"),
                    _auto("😭😭", reel="a", emotion="CRYING"),
                    sent=True, path=path)
    il.log_decision(_ctx(["🔥🔥", "goes hard 🔥", "W 🔥"], reel="b"),
                    _auto("🔥", reel="b", emotion="FIRE"),
                    sent=True, path=path)
    store = RagStore.load(path)
    assert len(store.examples) == 2
    # a fire-heavy reel should retrieve the fire example first
    hits = store.retrieve(_ctx(["🔥 insane", "🔥🔥 sheesh", "banger 🔥"]), k=1)
    assert hits and hits[0].reply == "🔥"


def test_rag_override_outranks_and_loads(tmp_path):
    path = str(tmp_path / "log.jsonl")
    ctx = _ctx(["😭", "😭😭", "😭 lol"], reel="a")
    il.log_decision(ctx, _auto("😭", reel="a"), sent=True, path=path)
    il.log_override(chat="c1", reel_id="a",
                    comment_sig=il._comment_hash("c1", ctx.comments),
                    bot_reply="😭", user_reply="LMAOO 💀", path=path)
    store = RagStore.load(path, approved_phrases=["LMAOO 💀"])
    # both the sent auto-reply and the override are usable examples
    replies = {e.reply for e in store.examples}
    assert "😭" in replies and "LMAOO 💀" in replies
    # the override borrowed the decision's emoji histogram
    ov = next(e for e in store.examples if e.is_override)
    assert ov.emoji_hist.get("😭")


def test_rag_drops_unapproved_contextual_override(tmp_path):
    path = str(tmp_path / "log.jsonl")
    ctx = _ctx(["😭", "😭😭", "😭 lol"], reel="a")
    il.log_decision(ctx, _auto("😭", reel="a"), sent=True, path=path)
    il.log_override(chat="c1", reel_id="a",
                    comment_sig=il._comment_hash("c1", ctx.comments),
                    bot_reply="😭", user_reply="LMAOO 💀", path=path)
    store = RagStore.load(path)  # no approved_phrases
    replies = {e.reply for e in store.examples}
    assert "😭" in replies          # bare emoji reply still usable
    assert "LMAOO 💀" not in replies  # text+emoji override discarded


def test_flagged_decision_not_used_as_style_example(tmp_path):
    path = str(tmp_path / "log.jsonl")
    flag = Decision(action=Action.FLAG,
                    flag=Flag(FlagKind.NO_CONSENSUS, "unclear"),
                    chat_name="c1", reel_id="r1")
    il.log_decision(_ctx(["a", "b", "c"]), flag, sent=False, path=path)
    store = RagStore.load(path)
    assert store.examples == []       # a flag is not a reply to imitate
