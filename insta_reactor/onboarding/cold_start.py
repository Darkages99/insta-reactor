"""Cold start — build a Profile from the user's OWN past replies, no wizard.

The product lives or dies on how little setup a first-time user needs. Instead of
asking them to type their favourite emojis and catchphrases (the `setup` wizard),
we read what they *already* send: the accessibility layer scrapes their own
outgoing DM bubbles once, and this module turns that raw message list into a
Profile automatically.

Everything here is pure and device-agnostic — it takes a `list[str]` of the
user's sent messages and returns a `Profile`, so it's fully unit-tested without a
phone. The device side only has to supply the strings (see
`Backend.iter_own_recent_messages`).

What we infer:
  * emoji_prefs   — the emojis they actually use, most-frequent first
  * reply_style   — single / double / text+emoji, from how their emoji messages
                    are shaped
  * common_replies— the short things they repeatedly type, verbatim
"""

from __future__ import annotations

from collections import Counter

from ..models import Profile, ReplyStyle
from ..engine.normalize import extract_emojis, strip_variation, is_trainable_reply

# Only short messages are "reactions" worth imitating; longer ones are real
# conversation, not a reusable catchphrase. Mirrors Settings.max_verbatim_len.
_MAX_REPLY_LEN = 24


def _infer_style(messages: list[str]) -> str:
    """Pick single/double/text_emoji from the shape of emoji-bearing messages."""
    single = double = text_emoji = 0
    for m in messages:
        emojis = extract_emojis(m)
        if not emojis:
            continue
        if any(ch.isalnum() for ch in m):
            text_emoji += 1                      # "bro 💀"
        elif len(emojis) >= 2:
            double += 1                          # "💀💀"
        else:
            single += 1                          # "💀"
    scores = {ReplyStyle.SINGLE: single, ReplyStyle.DOUBLE: double,
              ReplyStyle.TEXT_EMOJI: text_emoji}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else ReplyStyle.SINGLE


def derive_profile(messages: list[str], *, top_emojis: int = 8,
                   top_replies: int = 8, existing: Profile | None = None,
                   approved_phrases: list[str] | None = None) -> Profile:
    """Turn the user's own sent messages into a starting Profile.

    `existing` (if given) only contributes its extra_slang — the observed
    behaviour always wins for emojis/replies/style. Empty input yields an empty
    Profile (the caller can fall back to the wizard).

    `approved_phrases` gates what becomes a reusable `common_replies` entry
    (see engine.normalize.is_trainable_reply): only bare emoji reactions and
    phrases on this list are eligible, since anything else is too contextual
    to safely replay on an unrelated reel. emoji_prefs/reply_style are
    aggregate signals (not reproduced verbatim), so they use every message.
    """
    msgs = [m.strip() for m in messages if m and m.strip()]

    # --- emoji preferences: by frequency, tie-broken by first appearance -----
    counts: Counter = Counter()
    first_seen: dict[str, int] = {}
    for i, m in enumerate(msgs):
        for e in extract_emojis(m):
            b = strip_variation(e)
            counts[b] += 1
            first_seen.setdefault(b, i)
    emoji_prefs = [e for e, _ in sorted(
        counts.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))][:top_emojis]

    # --- common replies: short, frequently-typed messages, verbatim ----------
    reply_counts: Counter = Counter()
    canonical_form: dict[str, str] = {}
    for m in msgs:
        if len(m) > _MAX_REPLY_LEN:
            continue
        if not is_trainable_reply(m, approved_phrases):
            continue
        key = m.lower()
        reply_counts[key] += 1
        canonical_form.setdefault(key, m)     # keep the first-seen casing
    common_replies = [canonical_form[k] for k, _ in
                      reply_counts.most_common(top_replies)]

    return Profile(
        emoji_prefs=emoji_prefs,
        common_replies=common_replies,
        reply_style=_infer_style(msgs),
        extra_slang=dict(existing.extra_slang) if existing else {},
    )
