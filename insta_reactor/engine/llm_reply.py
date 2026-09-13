"""LLM reply layer — the sophisticated, personalized second opinion.

When the deterministic brain can't confidently pick a reaction (no clear crowd
consensus, or confidence below the bar) the runner can consult an LLM instead of
immediately handing the reel to you. This module builds that call:

  1. REDACT   — strip @handles and links from the comments before they leave the
     device (privacy + safety: we never forward a stranger's handle to the API).
  2. GROUND   — feed your style profile (favourite emojis, common replies, reply
     style) plus RAG few-shot examples of how you reacted to *similar* reels
     before, so the output sounds like you and not like a generic model.
  3. CONSTRAIN— demand a tiny JSON object back, parse it defensively, and reject
     anything that's too long, empty, or smuggles in an @mention/link.

`suggest_reply` returns an LlmSuggestion or None. None means "couldn't get a safe,
confident reply" -> the caller keeps the original flag (a human handles it). So
the LLM can only ever *upgrade* a flag into an auto-reply, never make things
worse. Nothing here raises.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..models import Profile, Settings, Comment
from .comment_filter import reactable
from .normalize import extract_emojis

log = logging.getLogger("insta_reactor.llm_reply")

# Same guard reply_select uses: an @mention or a link must never appear in an
# outgoing reply, and we also scrub it from what we send to the model.
_MENTION_OR_LINK = re.compile(r"(^|\s)@[\w.]+|https?://|www\.", re.IGNORECASE)


@dataclass
class LlmSuggestion:
    reply_text: str
    confidence: float
    source: str = "llm"


def _redact(text: str) -> str:
    """Remove @handles and links; collapse whitespace. Keeps emoji + words."""
    return _MENTION_OR_LINK.sub(" ", text or "").strip()


def _redacted_comments(comments: list[Comment], limit: int = 30) -> list[str]:
    out: list[str] = []
    for c in reactable(comments or []):
        red = _redact(c.text)
        if red:
            out.append(red)
        if len(out) >= limit:
            break
    return out


def build_prompt(ctx, profile: Profile, rag_examples: str,
                 grounding: str = "") -> tuple[str, str]:
    """Return (system, user) messages. Pure + inspectable for tests.

    `grounding`, if given, is a short line describing what the deterministic
    engine already inferred from the crowd (the winning emotion + how strong the
    consensus was). Feeding it in makes the model *synthesise* on top of a real
    signal instead of guessing from raw comments alone.
    """
    style_bits = []
    if profile.emoji_prefs:
        style_bits.append("favourite emojis (in order): "
                          + " ".join(profile.emoji_prefs[:8]))
    if profile.common_replies:
        style_bits.append("things you actually type: "
                          + ", ".join(f'"{r}"' for r in profile.common_replies[:8]))
    style_bits.append(f"reply style: {profile.reply_style}")
    style = "\n".join(f"- {b}" for b in style_bits)

    system = (
        "You are replying to an Instagram reel a friend sent, imitating ONE "
        "specific person's texting style. Reactions are short — usually just "
        "emoji or a couple of words, like a real DM reaction. Never write a "
        "sentence, never explain, never use @mentions or links. Match the "
        "person's style exactly.\n\n"
        "React to WHAT THE REEL IS ACTUALLY ABOUT (its caption/content) — not to "
        "the loudest comment. The crowd's comments are only a secondary hint at "
        "the vibe; never parrot a comment that doesn't fit the content. Match the "
        "reaction to the tone: hype slang like \"this goes crazy\" or 🔥 fits a "
        "flex/impressive/funny clip, but is WRONG for something informative, "
        "educational, wholesome, serious, or sad — react to those in a way that "
        "actually fits.\n\n"
        "Respond with ONLY a compact JSON object:\n"
        '{"reply": "<the reaction>", "confidence": <0..1>, '
        '"should_reply": <true|false>}\n'
        "Set should_reply=false (and confidence low) if the reel needs a real "
        "human reply (a question, something personal) or no short reaction fits."
    )
    comments = _redacted_comments(getattr(ctx, "comments", None) or [])
    caption = _redact(getattr(ctx, "caption", "") or "")
    caption_block = (f"WHAT THIS REEL IS ABOUT (the caption — your main signal):\n"
                     f"{caption}\n\n" if caption
                     else "WHAT THIS REEL IS ABOUT: (no caption available — infer "
                          "cautiously from the comments below)\n\n")
    ground_block = f"WHAT THE CROWD IS FEELING (from analysis):\n{grounding}\n\n" if grounding else ""
    user = (
        f"THIS PERSON'S STYLE:\n{style}\n\n"
        f"HOW THEY REACTED TO SIMILAR REELS BEFORE:\n{rag_examples}\n\n"
        f"{caption_block}"
        f"{ground_block}"
        f"THE CROWD'S COMMENTS (secondary vibe hint only, do NOT quote):\n"
        + "\n".join(f"- {c}" for c in comments[:30])
        + "\n\nGive their reaction as JSON."
    )
    return system, user


def _parse(raw: str, settings: Settings) -> Optional[LlmSuggestion]:
    """Parse+validate the model's JSON. Returns None if unusable/unsafe."""
    if not raw:
        return None
    # models sometimes wrap JSON in prose or fences — grab the first {...}.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("should_reply") is False:
        return None
    reply = obj.get("reply")
    if not isinstance(reply, str):
        return None
    reply = reply.strip()
    max_len = getattr(settings, "llm_max_reply_len", 40)
    if not reply or len(reply) > max_len:
        return None
    if _MENTION_OR_LINK.search(reply):
        return None
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return LlmSuggestion(reply_text=reply, confidence=conf)


def suggest_reply(ctx, profile: Profile, settings: Settings,
                  llm, rag_examples: str = "(no past examples yet)",
                  grounding: str = "") -> Optional[LlmSuggestion]:
    """Ask the LLM for a safe, in-style reaction. None => keep the human flag.

    `llm` is a ChatClient (llm/client.py). Requires at least one usable comment
    (an all-empty crowd gives the model nothing to react to). `grounding` is an
    optional line describing the deterministic crowd analysis (see build_prompt).
    Any failure, refusal, or unsafe/oversized output collapses to None.
    """
    if llm is None:
        return None
    has_caption = bool(_redact(getattr(ctx, "caption", "") or ""))
    if not has_caption and not _redacted_comments(getattr(ctx, "comments", None) or []):
        return None
    system, user = build_prompt(ctx, profile, rag_examples, grounding)
    try:
        raw = llm.complete(system, user,
                           temperature=0.7,
                           max_tokens=getattr(settings, "llm_max_tokens", 120))
    except Exception:
        log.exception("llm.complete raised")
        return None
    sugg = _parse(raw or "", settings)
    if sugg is None:
        return None
    floor = getattr(settings, "llm_min_confidence", 0.55)
    if sugg.confidence < floor:
        return None
    return sugg
