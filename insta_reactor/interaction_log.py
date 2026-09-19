"""Append-only interaction log — the corpus everything AI-native learns from.

One JSON object per line (`data/interaction_log.jsonl`), in the same
append-only spirit as the review queue. This is the single source of truth for:
  * RAG   — retrieve your past reels+replies as few-shot context (rag/store.py),
  * cold-start / correction learning — fold your real behaviour back into the
    profile,
  * the eval harness — replay old contexts through the engine and measure drift.

Privacy posture (matches docs/V3_PLAN.md "nothing leaves the device"):
  * YOUR OWN output — the reply text, its source, the winning emotion,
    confidence — is stored in full. It's your data and it's what personalization
    needs.
  * OTHER PEOPLE'S comment text is NOT stored verbatim by default. We store a
    stable hash (to correlate records) plus a NON-identifying emoji histogram +
    counts, which is all the retriever actually needs. Pass log_raw_text=True
    (a debug/opt-in flag) to also store the raw comments for inspection.

`log_decision`/`log_override` never raise — a logging failure must not abort a
run. Each line is timestamped and carries a run_id so an override can be tied
back to the decision it corrected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Iterable, Iterator, Optional

from .engine.comment_filter import reactable
from .engine.normalize import extract_emojis, strip_variation
from .paths import data_path

log = logging.getLogger("insta_reactor.interaction_log")

DEFAULT_LOG_PATH = data_path("interaction_log.jsonl")


def _comment_hash(chat_name: str, comments) -> str:
    texts = sorted({(c.text or "").strip() for c in reactable(comments or [])})
    joined = "".join(texts)
    return hashlib.sha1(f"{chat_name}{joined}".encode("utf-8")).hexdigest()[:16]


def _emoji_histogram(comments) -> dict[str, int]:
    """Emoji -> count across the comments. Emoji are non-identifying, so this is
    safe to store and is the main lexical feature the retriever ranks on."""
    hist: dict[str, int] = {}
    for c in reactable(comments or []):
        for e in extract_emojis(c.text or ""):
            b = strip_variation(e)
            hist[b] = hist.get(b, 0) + 1
    return hist


def build_record(ctx, decision, *, sent: bool,
                 run_id: str = "", log_raw_text: bool = False) -> dict:
    """Build (but don't write) the log record for a single decision."""
    comments = ctx.comments if ctx is not None else None
    rec: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "kind": "decision",
        "chat": getattr(decision, "chat_name", "") or getattr(ctx, "chat_name", ""),
        "reel_id": getattr(decision, "reel_id", "") or getattr(ctx, "reel_id", ""),
        "comment_sig": _comment_hash(getattr(ctx, "chat_name", ""), comments),
        "comment_count": len(comments) if comments else 0,
        "emoji_hist": _emoji_histogram(comments),
        "action": getattr(decision, "action", ""),
        "reply_text": getattr(decision, "reply_text", None),
        "reply_source": getattr(decision, "reply_source", None),
        "winning_emotion": getattr(decision, "winning_emotion", None),
        "confidence": round(float(getattr(decision, "confidence", 0.0) or 0.0), 4),
        "flag_kind": (decision.flag.kind if getattr(decision, "flag", None) else None),
        "sent": bool(sent),
    }
    if log_raw_text and comments:
        rec["comments_raw"] = [
            {"text": c.text, "likes": int(c.likes or 0)} for c in comments]
    return rec


def _append(path: str, rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("failed to append interaction log")


def log_decision(ctx, decision, *, sent: bool, run_id: str = "",
                 path: str = DEFAULT_LOG_PATH, log_raw_text: bool = False) -> None:
    """Record one engine decision (auto-reply or flag). Never raises."""
    try:
        _append(path, build_record(ctx, decision, sent=sent, run_id=run_id,
                                   log_raw_text=log_raw_text))
    except Exception:
        log.exception("log_decision failed")


def log_override(*, chat: str, reel_id: str, comment_sig: str,
                 bot_reply: Optional[str], user_reply: str,
                 run_id: str = "", path: str = DEFAULT_LOG_PATH) -> None:
    """Record that the human sent something different from the bot's choice.

    This is the ground-truth label the correction-learning + eval harness use:
    `bot_reply` is what the engine proposed (or None if it flagged/skipped),
    `user_reply` is what you actually sent. Never raises.
    """
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "kind": "override",
        "chat": chat,
        "reel_id": reel_id,
        "comment_sig": comment_sig,
        "bot_reply": bot_reply,
        "user_reply": user_reply,
    }
    try:
        _append(path, rec)
    except Exception:
        log.exception("log_override failed")


def read_all(path: str = DEFAULT_LOG_PATH) -> Iterator[dict]:
    """Yield every well-formed record. Silently skips corrupt lines."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def read_kind(kind: str, path: str = DEFAULT_LOG_PATH) -> list[dict]:
    """All records of a given `kind` ("decision" | "override")."""
    return [r for r in read_all(path) if r.get("kind") == kind]
