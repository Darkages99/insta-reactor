"""Choose the actual outgoing reaction text once the brain has decided to reply.

Order of preference (per the product spec):

  1. **Very popular comment, sent as-is.** If the crowd has coalesced around one
     comment (high like count / dominant share of likes) and it's short enough
     to echo, we just send that comment back — it's already the perfect reaction.
  2. **A favourite emoji the crowd is using.** Otherwise, if any emoji in the
     comments is also in *your* favourites list, reply with that (preferring one
     that matches the winning emotion). This makes replies feel picked by you.
  3. **Emotion fallback.** Failing both, build a reply from the winning emotion
     in your usual style (the original behaviour).

Pure/deterministic so it is fully unit-tested without a device.
"""

from __future__ import annotations

import hashlib
import re

from ..models import Profile, Settings, Comment, ReplyStyle
from .comment_filter import reactable
from .normalize import extract_emojis, strip_variation
from .slang_map import EMOJI_EMOTION
from . import scoring


# A comment that @-mentions or tags another account, or links out. Echoing it
# verbatim would tag a stranger into your DM (or forward a link) as if it were
# your own reaction — never safe, however popular the comment is.
_MENTION_OR_LINK = re.compile(r"(^|\s)@[\w.]+|https?://|www\.", re.IGNORECASE)


def _most_popular_sendable(comments: list[Comment], settings: Settings) -> Comment | None:
    """Return a comment popular+short enough to echo verbatim, else None."""
    if not comments:
        return None
    total_likes = sum(max(0, int(c.likes or 0)) for c in comments)
    best = max(comments, key=lambda c: max(0, int(c.likes or 0)))
    likes = max(0, int(best.likes or 0))
    if likes <= 0:
        return None
    text = best.text.strip()
    if len(text) > settings.max_verbatim_len:
        return None
    if "?" in text:
        # A question is commentary about the video ("wait is that the..."),
        # not a generic reaction — never safe to echo as if it's your own take.
        return None
    if _MENTION_OR_LINK.search(text):
        # Echoing a comment that tags another account (or links out) would
        # @-mention a stranger into your DM as if you wrote it — never safe.
        return None
    if not extract_emojis(text):
        # Plain worded text ("How many times bro") reads as a specific,
        # possibly out-of-character take even when short. Repeated/emphatic
        # emoji reactions ("😢😢😢") are the safe generic case — require at
        # least one emoji to qualify for a verbatim echo, else fall through
        # to the favourite-emoji tier.
        return None
    by_floor = likes >= settings.popular_min_likes
    # Share path needs a small absolute floor so "1 of 1 like" can't qualify.
    by_share = (
        total_likes > 0
        and likes >= 10
        and likes / total_likes >= settings.popular_like_share
    )
    return best if (by_floor or by_share) else None


def _favourite_emojis_in_comments(
    comments: list[Comment], profile: Profile
) -> list[tuple[str, str]]:
    """Favourites (in preference order) that actually appear in the comments,
    as (favourite_emoji, its_emotion) pairs."""
    present: set[str] = set()
    for c in comments:
        present.update(extract_emojis(c.text))
    matches: list[tuple[str, str]] = []
    for fav in profile.emoji_prefs:
        emojis = extract_emojis(fav)
        if not emojis:
            continue
        base = emojis[0]
        if base in present:
            matches.append((fav, EMOJI_EMOTION.get(base, base)))
    return matches


def _build_with_emoji(emoji: str, profile: Profile) -> str:
    """Style-aware reply built around a specific emoji."""
    if profile.reply_style == ReplyStyle.DOUBLE:
        return emoji * 2
    if profile.reply_style == ReplyStyle.TEXT_EMOJI:
        base = strip_variation(extract_emojis(emoji)[0]) if extract_emojis(emoji) else emoji
        emotion = EMOJI_EMOTION.get(base, base)
        for reply in profile.common_replies:
            if scoring._reply_emotion(reply, profile) == emotion:
                return reply
        return f"bro {emoji}"
    return emoji  # single


def select_reply(
    ctx, winner: str, profile: Profile, settings: Settings
) -> tuple[str, str]:
    """Return (reply_text, reply_source)."""
    comments = reactable(ctx.comments)

    # 1) very popular comment -> echo verbatim
    pop = _most_popular_sendable(comments, settings)
    if pop is not None:
        return pop.text.strip(), "popular_verbatim"

    # 2) a favourite emoji the crowd is also using
    if settings.prefer_favourite_emoji:
        matches = _favourite_emojis_in_comments(comments, profile)
        # prefer a favourite matching the winning emotion, else the top favourite
        for fav, emotion in matches:
            if emotion == winner:
                return _build_with_emoji(fav, profile), "favourite_match"
        if matches:
            return _build_with_emoji(matches[0][0], profile), "favourite_match"

    # 3) emotion fallback (original behaviour)
    return scoring.build_reply(winner, profile), "emotion"


# ---------------------------------------------------------------------------
# Reply variety
# ---------------------------------------------------------------------------
# Reactions that are pure emoji get stylistically varied so we don't send the
# exact same thing to every reel (which reads as a bot). We vary the COUNT
# (1..MAX) and, when the crowd is leaning on an emoji that isn't in your
# favourites, blend it with your favourite. We never send the same reply 3+
# times in a row. popular_verbatim is left untouched — it's a real crowd comment
# we echo as-is, not something to restyle.

# sources whose (emoji) reply we may restyle
_DIVERSIFY_SOURCES = {"favourite_match", "emotion"}
MAX_EMOJI_REPEAT = 5   # user wants the emoji count to vary between 1 and 5


def _is_pure_emoji(text: str) -> bool:
    """True if `text` is only emoji (and whitespace) — no letters/digits. Keeps
    us from mangling text replies like 'bro 💀'."""
    return bool(text) and bool(extract_emojis(text)) and not any(
        ch.isalnum() for ch in text)


def _emoji_counts(comments: list[Comment]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in comments:
        for e in extract_emojis(c.text):
            b = strip_variation(e)
            counts[b] = counts.get(b, 0) + 1
    return counts


def _mix_emoji(comments: list[Comment], profile: Profile, primary: str) -> str | None:
    """The most common comment emoji that is NOT one of your favourites and
    differs from `primary` — the 'other' emoji to blend in (per the user's
    request to mix a list-emoji with a common non-list one). Must actually be
    common (appears >= 2x) so we don't blend in a one-off."""
    fav_bases = {strip_variation(extract_emojis(f)[0])
                 for f in profile.emoji_prefs if extract_emojis(f)}
    p = strip_variation(primary)
    for emoji, n in sorted(_emoji_counts(comments).items(),
                           key=lambda kv: (-kv[1], kv[0])):
        if emoji != p and emoji not in fav_bases and n >= 2:
            return emoji
    return None


def _variety_seed(comments: list[Comment]) -> int:
    joined = "".join(sorted({c.text.strip() for c in comments}))[:200]
    return int(hashlib.sha1(joined.encode("utf-8")).hexdigest(), 16)


def diversify_reply(reply_text: str, reply_source: str,
                    comments: list[Comment], profile: Profile,
                    settings: Settings, recent: list[str]) -> str:
    """Give a pure-emoji reply natural variety.

    - varies the emoji count between 1 and MAX_EMOJI_REPEAT,
    - blends in a common non-favourite comment emoji when one exists,
    - never produces a reply identical to the previous *two* in `recent`
      (so at most 2 identical replies in a row).

    Deterministic given its inputs (the length/mix choice is seeded from the
    comment set), so it's fully unit-testable; `recent` is the list of replies
    already sent this run, supplied by the runner. Non-emoji or verbatim replies
    (e.g. an echoed popular comment, or 'bro 💀') pass through unchanged.
    """
    if reply_source not in _DIVERSIFY_SOURCES or not _is_pure_emoji(reply_text):
        return reply_text
    primary = extract_emojis(reply_text)[0]
    comments = reactable(comments)
    mix = _mix_emoji(comments, profile, primary)
    seed = _variety_seed(comments)

    def build(length: int, use_mix: bool) -> str:
        length = max(1, min(MAX_EMOJI_REPEAT, length))
        if use_mix and mix:
            return (primary * (length - 1) + mix) if length >= 2 else primary + mix
        return primary * length

    length = 1 + (seed % MAX_EMOJI_REPEAT)                        # 1..MAX
    use_mix = mix is not None and (seed // MAX_EMOJI_REPEAT) % 3 != 0  # ~2/3 when available
    reply = build(length, use_mix)

    # enforce "no more than 2 identical in a row": if this would be the third
    # identical reply, cycle the length (and flip the mix each full cycle) until
    # it differs. The MAX distinct lengths guarantee we find a different reply.
    for _ in range(2 * MAX_EMOJI_REPEAT):
        if not (len(recent) >= 2 and recent[-1] == reply and recent[-2] == reply):
            break
        length = 1 + (length % MAX_EMOJI_REPEAT)
        if length == 1 and mix:
            use_mix = not use_mix
        reply = build(length, use_mix)
    return reply
