"""Weighted voting, personal bias, and confidence.

Implements the spec's arithmetic:

    final_score = public_score * 0.4  +  my_preference_score * 0.6

plus like-weighting of comments and a confidence measure derived from how
clearly the *public* agrees (not from the personally-biased result).
"""

from __future__ import annotations

import math
from collections import Counter

from ..models import Comment, Profile, Settings, Emotion
from ..models import CANON_EMOJI
from .normalize import signals_for_comment, extract_emojis, strip_variation
from .slang_map import EMOJI_EMOTION


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def like_weight(likes: int, mode: str) -> float:
    """Extra weight a comment earns from its like count."""
    likes = max(0, int(likes or 0))
    if mode == "none":
        return 1.0
    if mode == "linear":
        return 1.0 + likes / 50.0
    # default: diminishing returns so one viral comment can't own the vote
    return 1.0 + math.log1p(likes)


def _normalize(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


# --------------------------------------------------------------------------
# public score (the crowd)
# --------------------------------------------------------------------------

def public_scores(
    comments: list[Comment],
    settings: Settings,
    extra_slang: dict[str, str] | None = None,
    model: "object | None" = None,
) -> dict[str, float]:
    """Weighted, per-comment-capped emotion totals across all comments.

    When `model` (an EmotionClassifier) is supplied, each comment's rule signal
    is blended with the model's per-comment distribution:

        blended = (1 - w) * rule_dist  +  w * model_dist,  w = ensemble_model_weight

    The rule side keeps DEAD/FIRE strong (the model has no class for them); the
    model side covers the free-text comments the dictionary can't parse. With
    `model=None` this is byte-for-byte the original rules-only behaviour.
    """
    raw: Counter = Counter()
    cap = settings.per_comment_cap

    model_dists = None
    if model is not None:
        model_dists = model.classify_batch([c.text for c in comments])
        mw = min(1.0, max(0.0, settings.ensemble_model_weight))
        rw = 1.0 - mw

    for i, c in enumerate(comments):
        w = like_weight(c.likes, settings.like_weight_mode)
        sig = signals_for_comment(c.text, extra_slang)
        if model_dists is None:
            for emotion, count in sig.items():
                raw[emotion] += min(count, cap) * w
            continue
        # ensemble path: blend two per-comment distributions
        rule_dist = _normalize({e: min(cnt, cap) for e, cnt in sig.items()})
        model_dist = model_dists[i] if i < len(model_dists) else {}
        for e in set(rule_dist) | set(model_dist):
            raw[e] += (rw * rule_dist.get(e, 0.0)
                       + mw * model_dist.get(e, 0.0)) * w
    return dict(raw)


# --------------------------------------------------------------------------
# personal score (you)
# --------------------------------------------------------------------------

def personal_scores(profile: Profile) -> dict[str, float]:
    """Distribution over emotions derived from your stated preferences.

    - Ranked emoji preferences decay geometrically (primary counts most).
    - Common replies are parsed and added with a smaller weight so your typed
      style ("nah 😭", "LMAOO") also nudges the result.
    Unknown emojis are kept as their own emotion key so they can still win.
    """
    raw: Counter = Counter()

    decay = 0.6
    for i, emoji in enumerate(profile.emoji_prefs):
        base = strip_variation(emoji)
        # a preference entry could be multi-char; take its first emoji if so
        emojis = extract_emojis(emoji)
        key = None
        if emojis:
            key = EMOJI_EMOTION.get(emojis[0], emojis[0])
        elif base:
            key = EMOJI_EMOTION.get(base, base)
        if key:
            raw[key] += decay ** i

    for reply in profile.common_replies:
        sig = signals_for_comment(reply, profile.extra_slang)
        for emotion, count in sig.items():
            raw[emotion] += 0.4 * min(count, 2)

    return dict(raw)


# --------------------------------------------------------------------------
# combine + confidence
# --------------------------------------------------------------------------

def combine(
    public_norm: dict[str, float],
    personal_norm: dict[str, float],
    settings: Settings,
) -> dict[str, float]:
    keys = set(public_norm) | set(personal_norm)
    return {
        k: settings.public_weight * public_norm.get(k, 0.0)
        + settings.personal_weight * personal_norm.get(k, 0.0)
        for k in keys
    }


def _top_two(scores: dict[str, float]) -> tuple[tuple[str, float], tuple[str, float]]:
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top = ordered[0] if ordered else (None, 0.0)
    second = ordered[1] if len(ordered) > 1 else (None, 0.0)
    return top, second


def consensus_metrics(public_norm: dict[str, float]) -> tuple[str | None, float, float]:
    """Return (public_winner, top_share, margin) from the crowd distribution."""
    (w, ws), (_, ss) = _top_two(public_norm)
    return w, ws, (ws - ss)


def confidence_score(
    top_share: float,
    margin: float,
    n_comments: int,
    settings: Settings,
) -> float:
    """Blend of consensus strength, winning margin, and sample volume -> [0,1]."""
    volume = min(1.0, n_comments / max(1, settings.volume_full_at))
    margin_norm = min(1.0, margin / settings.margin_full_at) if settings.margin_full_at else 0.0
    conf = (
        settings.conf_w_top_share * top_share
        + settings.conf_w_volume * volume
        + settings.conf_w_margin * margin_norm
    )
    return round(min(1.0, max(0.0, conf)), 4)


# --------------------------------------------------------------------------
# reply text generation (sounds like you)
# --------------------------------------------------------------------------

def _reply_emotion(reply_text: str, profile: Profile) -> str | None:
    sig = signals_for_comment(reply_text, profile.extra_slang)
    if not sig:
        return None
    return max(sig.items(), key=lambda kv: kv[1])[0]


def emoji_for_emotion(emotion: str, profile: Profile) -> str:
    """Prefer one of *your* emojis that expresses this emotion; else canon."""
    for pref in profile.emoji_prefs:
        emojis = extract_emojis(pref)
        if emojis and EMOJI_EMOTION.get(emojis[0], emojis[0]) == emotion:
            return pref
    if emotion in CANON_EMOJI:
        return CANON_EMOJI[emotion]
    # emotion key *is* a raw emoji (unknown-but-preferred case)
    return emotion


def build_reply(emotion: str, profile: Profile) -> str:
    """Produce the actual outgoing message in your style for this emotion."""
    emoji = emoji_for_emotion(emotion, profile)

    if profile.reply_style == "text_emoji":
        # reuse an actual common reply that matches this emotion, if any
        for reply in profile.common_replies:
            if _reply_emotion(reply, profile) == emotion:
                return reply
        # otherwise pair a neutral filler with the emoji
        return f"bro {emoji}"

    if profile.reply_style == "double":
        return emoji * 2

    # single (default): prefer a bare-emoji common reply, else the emoji
    for reply in profile.common_replies:
        if reply.strip() and _reply_emotion(reply, profile) == emotion:
            # a one-token reply like "💀" or "LMAOO"
            if len(reply.split()) == 1:
                return reply.strip()
    return emoji
