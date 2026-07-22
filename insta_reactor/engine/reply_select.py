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
