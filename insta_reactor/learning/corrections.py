"""Correction learning — turn what you actually did into what the bot does next.

The onboarding profile is a starting guess; your real behaviour is the ground
truth. Every time you send something different from the bot's choice (or reply to
a reel it flagged), the runner logs an `override` record. This module folds those
overrides back into the Profile and gently auto-tunes the confidence bar — with
no screens, no labelling, no babysitting.

All pure functions over log records + a Profile/Settings, so they're fully
unit-tested and side-effect-free; the caller decides whether to persist the
result.

Two knobs move:
  * `learn_profile`  — promotes emojis you keep correcting toward, adds your
    repeated phrasings to common_replies, and switches reply_style if your
    corrections clearly favour a different one.
  * `adapt_settings` — as agreement between the bot's proposals and what you
    actually send rises (with enough samples), it lowers auto_reply_min_confidence
    toward a floor so fewer reels get needlessly flagged; if agreement is poor it
    raises the bar. Movement is damped and bounded so it can't swing wildly.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from ..models import Profile, Settings, ReplyStyle
from ..engine.normalize import extract_emojis, strip_variation, is_trainable_reply

_MAX_REPLY_LEN = 24


def _override_replies(overrides: list[dict]) -> list[str]:
    return [(o.get("user_reply") or "").strip()
            for o in overrides if (o.get("user_reply") or "").strip()]


def _emoji_base(pref: str) -> str:
    ems = extract_emojis(pref)
    return strip_variation(ems[0]) if ems else pref


def _infer_style(replies: list[str], current: str, *,
                 min_samples: int = 5, dominance: float = 0.6) -> str:
    single = double = text = 0
    for m in replies:
        emojis = extract_emojis(m)
        if not emojis:
            continue
        if any(ch.isalnum() for ch in m):
            text += 1
        elif len(emojis) >= 2:
            double += 1
        else:
            single += 1
    total = single + double + text
    if total < min_samples:
        return current
    scores = {ReplyStyle.SINGLE: single, ReplyStyle.DOUBLE: double,
              ReplyStyle.TEXT_EMOJI: text}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] / total >= dominance else current


def learn_profile(profile: Profile, overrides: list[dict], *,
                  max_emojis: int = 10, max_replies: int = 12,
                  min_reply_count: int = 2,
                  approved_phrases: list[str] | None = None) -> Profile:
    """Return a new Profile with the overrides folded in (input unchanged)."""
    replies = _override_replies(overrides)
    if not replies:
        return profile

    # --- emoji promotion: existing rank + double weight for corrected emojis --
    corrected: Counter = Counter()
    for r in replies:
        for e in extract_emojis(r):
            corrected[strip_variation(e)] += 1
    score: dict[str, float] = {}
    prefs = [_emoji_base(e) for e in profile.emoji_prefs]
    n = len(prefs)
    for i, e in enumerate(prefs):
        score[e] = float(n - i)                 # keep current ordering as a prior
    for e, c in corrected.items():
        score[e] = score.get(e, 0.0) + 2.0 * c  # corrections weigh double
    new_prefs = [e for e, _ in sorted(score.items(), key=lambda kv: -kv[1])][:max_emojis]

    # --- common replies: add your repeated corrections, verbatim --------------
    reply_counts = Counter(r.lower() for r in replies)
    new_common = list(profile.common_replies)
    seen = {c.lower() for c in new_common}
    for r in replies:
        low = r.lower()
        if (reply_counts[low] >= min_reply_count and low not in seen
                and len(r) <= _MAX_REPLY_LEN
                and is_trainable_reply(r, approved_phrases)):
            new_common.append(r)
            seen.add(low)
    new_common = new_common[:max_replies]

    return Profile(
        emoji_prefs=new_prefs,
        common_replies=new_common,
        reply_style=_infer_style(replies, profile.reply_style),
        extra_slang=dict(profile.extra_slang),
    )


def agreement_rate(records: list[dict]) -> float | None:
    """Fraction of the bot's proposed replies you did NOT override.

    Proposals = logged auto_reply decisions; disagreements = override records
    that carry a bot_reply (i.e. you changed a reply it proposed). Returns None
    when the bot has proposed nothing yet.
    """
    proposed = sum(1 for r in records
                   if r.get("kind") == "decision" and r.get("action") == "auto_reply")
    if proposed == 0:
        return None
    disagreements = sum(1 for r in records
                        if r.get("kind") == "override" and r.get("bot_reply"))
    disagreements = min(disagreements, proposed)
    return round((proposed - disagreements) / proposed, 4)


def adapt_settings(settings: Settings, agreement: float | None, n_samples: int,
                   *, min_samples: int = 20, floor: float = 0.50,
                   cap: float = 0.75, damping: float = 0.5) -> Settings:
    """Nudge auto_reply_min_confidence based on measured agreement.

    High agreement (you rarely correct the bot) => lower the bar toward `floor`
    so fewer reels get flagged; low agreement => raise it toward `cap`. Requires
    at least `min_samples` proposals before it moves at all, and only steps
    `damping` of the way to the target so it eases in. Returns a new Settings.
    """
    if agreement is None or n_samples < min_samples:
        return settings
    a = max(0.60, min(0.95, agreement))
    frac = (a - 0.60) / (0.95 - 0.60)           # 0 at .60 agreement, 1 at .95
    target = cap - frac * (cap - floor)          # high agreement -> near floor
    cur = settings.auto_reply_min_confidence
    new = cur + damping * (target - cur)
    new = round(max(floor, min(cap, new)), 4)
    return replace(settings, auto_reply_min_confidence=new)
