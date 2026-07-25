"""Reply variety (diversify_reply): vary emoji count 1..5, blend in a common
non-favourite emoji, and never send the same reaction 3x in a row.

This addresses the live observation that 5 consecutive laugh-reels all got the
identical single '😂'. These tests are pure/deterministic (no device)."""

from __future__ import annotations

from insta_reactor.models import Profile, Settings, Comment, ReplyStyle
from insta_reactor.engine.reply_select import (
    diversify_reply, _mix_emoji, _is_pure_emoji, MAX_EMOJI_REPEAT,
)
from insta_reactor.engine.normalize import extract_emojis

LAUGH = "😂"
EYES = "👀"     # common in comments, NOT in the profile list
FIRE = "🔥"

PROFILE = Profile(emoji_prefs=[LAUGH, "😭", FIRE],
                  common_replies=["lmao"], reply_style=ReplyStyle.SINGLE)
SET = Settings()


def _comments(*texts):
    return [Comment(text=t, likes=1) for t in texts]


def _laugh_comments(tag=""):
    # a laugh-dominant reel that also has a common non-favourite emoji (👀)
    return _comments(*([f"{LAUGH} lol {tag}"] * 6 + [f"{EYES} {tag}"] * 3
                       + [f"dead {tag}"] * 2))


class TestPassthrough:
    def test_verbatim_reply_unchanged(self):
        # an echoed popular comment must never be restyled
        r = diversify_reply("😭😭😭", "popular_verbatim", _laugh_comments(),
                            PROFILE, SET, [])
        assert r == "😭😭😭"

    def test_text_reply_unchanged(self):
        r = diversify_reply("bro 💀", "favourite_match", _laugh_comments(),
                            PROFILE, SET, [])
        assert r == "bro 💀"

    def test_is_pure_emoji(self):
        assert _is_pure_emoji("😂😂")
        assert not _is_pure_emoji("bro 💀")
        assert not _is_pure_emoji("")


class TestVariety:
    def test_output_is_valid_emoji_run(self):
        r = diversify_reply(LAUGH, "favourite_match", _laugh_comments(),
                            PROFILE, SET, [])
        got = extract_emojis(r)
        assert 1 <= len(got) <= MAX_EMOJI_REPEAT
        assert set(got) <= {LAUGH, EYES}          # primary and/or the mix emoji
        assert LAUGH in got                        # always keeps the favourite

    def test_deterministic(self):
        a = diversify_reply(LAUGH, "favourite_match", _laugh_comments("x"),
                            PROFILE, SET, [])
        b = diversify_reply(LAUGH, "favourite_match", _laugh_comments("x"),
                            PROFILE, SET, [])
        assert a == b

    def test_mix_emoji_is_common_nonfavourite(self):
        assert _mix_emoji(_laugh_comments(), PROFILE, LAUGH) == EYES
        # a one-off non-favourite emoji is NOT blended in (needs >= 2)
        rare = _comments(LAUGH, LAUGH, LAUGH, "🥶 once")
        assert _mix_emoji(rare, PROFILE, LAUGH) is None

    def test_never_three_identical_in_a_row(self):
        c = _laugh_comments()
        r0 = diversify_reply(LAUGH, "favourite_match", c, PROFILE, SET, [])
        # if the previous two sent replies were r0, the next must differ
        r1 = diversify_reply(LAUGH, "favourite_match", c, PROFILE, SET, [r0, r0])
        assert r1 != r0

    def test_five_consecutive_reels_are_not_all_identical(self):
        """The live complaint: 5 laugh reels -> 5x '😂'. With variety, the run of
        five must not be all the same, and must never repeat 3x in a row."""
        recent: list[str] = []
        sent: list[str] = []
        for i in range(5):
            # each reel has a slightly different comment set (different seed),
            # as real reels do.
            r = diversify_reply(LAUGH, "favourite_match", _laugh_comments(str(i)),
                                PROFILE, SET, recent)
            recent.append(r)
            sent.append(r)
        assert len(set(sent)) > 1, f"all identical: {sent}"
        for i in range(len(sent) - 2):
            assert not (sent[i] == sent[i + 1] == sent[i + 2]), \
                f"3 identical in a row at {i}: {sent}"
