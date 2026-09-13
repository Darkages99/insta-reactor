"""RAG over your own interaction history — zero-cost lexical retrieval.

The LLM reply layer gets dramatically more personal when it can see *how you
actually reacted to similar reels before*. This store turns the interaction log
into that few-shot memory without any embedding API cost: it ranks past
examples against the current reel by

    similarity = cosine(emoji_histogram)          # what the crowd was doing
               + emotion_match_bonus              # same winning emotion
               + count_proximity                  # similar-sized comment crowd

Only records that produced a real reply are usable as style examples:
  * `override` records (what you actually typed after correcting the bot) are
    the gold standard and get a recency/quality weight boost;
  * `decision` records that were sent auto-replies are good positives too.

The retriever is deliberately pluggable — swap `_similarity` for an embeddings
call later without touching callers. Nothing here hits the network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..interaction_log import DEFAULT_LOG_PATH, read_all, _emoji_histogram
from ..engine.normalize import is_trainable_reply


@dataclass
class Example:
    """One retrieved past interaction, ready to render as a few-shot line."""
    emoji_hist: dict[str, int]
    winning_emotion: Optional[str]
    reply: str
    is_override: bool = False
    score: float = 0.0


def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class RagStore:
    """In-memory index of usable style examples loaded from the interaction log."""

    examples: list[Example] = field(default_factory=list)

    @classmethod
    def load(cls, path: str = DEFAULT_LOG_PATH,
             approved_phrases: list[str] | None = None) -> "RagStore":
        # correlate override replies back to the decision that has the emoji
        # histogram (overrides don't carry one), keyed by comment_sig.
        # `approved_phrases` gates which replies are safe to few-shot back into
        # the LLM prompt: bare emoji reactions and pre-approved phrases only
        # (see engine.normalize.is_trainable_reply) — contextual replies would
        # get reproduced verbatim on an unrelated reel otherwise.
        hist_by_sig: dict[str, dict[str, int]] = {}
        emo_by_sig: dict[str, Optional[str]] = {}
        examples: list[Example] = []
        for rec in read_all(path):
            if rec.get("kind") == "decision":
                sig = rec.get("comment_sig", "")
                if rec.get("emoji_hist"):
                    hist_by_sig[sig] = rec["emoji_hist"]
                    emo_by_sig[sig] = rec.get("winning_emotion")
                reply = rec.get("reply_text")
                if rec.get("sent") and reply and is_trainable_reply(reply, approved_phrases):
                    examples.append(Example(
                        emoji_hist=rec.get("emoji_hist", {}),
                        winning_emotion=rec.get("winning_emotion"),
                        reply=reply))
        # second pass: overrides become high-value examples, borrowing the
        # histogram/emotion from the decision they corrected.
        for rec in read_all(path):
            if (rec.get("kind") == "override" and rec.get("user_reply")
                    and is_trainable_reply(rec["user_reply"], approved_phrases)):
                sig = rec.get("comment_sig", "")
                examples.append(Example(
                    emoji_hist=hist_by_sig.get(sig, {}),
                    winning_emotion=emo_by_sig.get(sig),
                    reply=rec["user_reply"],
                    is_override=True))
        return cls(examples=examples)

    def _similarity(self, hist: dict[str, int], emotion: Optional[str],
                    ex: Example) -> float:
        sim = _cosine(hist, ex.emoji_hist)
        if emotion and ex.winning_emotion and emotion == ex.winning_emotion:
            sim += 0.25
        if ex.is_override:      # your explicit corrections matter most
            sim += 0.15
        return sim

    def retrieve(self, ctx, k: int = 5) -> list[Example]:
        """Top-k most similar past examples for this reel context."""
        hist = _emoji_histogram(getattr(ctx, "comments", None))
        emotion = None  # ctx has no emotion yet; histogram carries the signal
        scored: list[Example] = []
        for ex in self.examples:
            s = self._similarity(hist, emotion, ex)
            if s <= 0:
                continue
            scored.append(Example(ex.emoji_hist, ex.winning_emotion, ex.reply,
                                  ex.is_override, score=round(s, 4)))
        scored.sort(key=lambda e: e.score, reverse=True)
        return scored[:k]

    @staticmethod
    def format_examples(examples: list[Example]) -> str:
        """Render retrieved examples as compact few-shot lines for the prompt."""
        if not examples:
            return "(no past examples yet)"
        lines = []
        for ex in examples:
            crowd = " ".join(f"{e}×{n}" for e, n in
                             sorted(ex.emoji_hist.items(), key=lambda kv: -kv[1])[:6])
            lines.append(f"- crowd used [{crowd or 'n/a'}] -> you replied "
                         f"\"{ex.reply}\"")
        return "\n".join(lines)
