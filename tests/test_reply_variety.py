"""Reply variety (diversify_reply): vary emoji count 1..4, blend in a related
emoji (same emotional family — crying/skull/other laughs — or a common
non-favourite emoji the crowd is using), and never send the same reaction 3x in
a row.

This addresses the live observation that consecutive laugh-reels all got the
identical single '😂' (and the paired '😂😂'). These tests are pure/
deterministic (no device)."""

from __future__ import annotations

from insta_reactor.models import Profile, Settings, Comment, ReplyStyle
from insta_reactor.engine.reply_select import (
    diversify_reply, _blend_pool, _is_pure_emoji,
    MAX_EMOJI_REPEAT, RELATED_EMOJI,
)
from insta_reactor.engine.normalize import extract_emojis, strip_variation
from insta_reactor.models import Emotion

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

    def test_llm_worded_reply_unchanged(self):
        # the LLM authors most replies live; its WORDED replies must pass through
        r = diversify_reply("nah that's crazy 💀", "llm", _laugh_comments(),
                            PROFILE, SET, [])
        assert r == "nah that's crazy 💀"

    def test_llm_pure_emoji_reply_is_diversified(self):
        # the live bug: llm returns '😂😂' every laugh reel. Pure-emoji llm
        # replies must get the same variety treatment (never 3x identical).
        c = _laugh_comments()
        r0 = diversify_reply("😂😂", "llm", c, PROFILE, SET, [])
        assert LAUGH in extract_emojis(r0)
        r1 = diversify_reply("😂😂", "llm", c, PROFILE, SET, [r0, r0])
        assert r1 != r0                         # not a third identical in a row

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
        # the run is the primary plus at most one blended accent from the pool
        # (a related-emotion emoji or the common crowd emoji).
        allowed = {strip_variation(LAUGH)} | {
            strip_variation(e) for e in _blend_pool(LAUGH, _laugh_comments())}
        assert {strip_variation(g) for g in got} <= allowed
        assert LAUGH in got                        # primary stays dominant
        assert len({strip_variation(g) for g in got}) <= 2   # primary + 1 accent

    def test_blend_pool_has_related_emotion_emojis(self):
        # a laugh reaction should be able to blend crying/skull (dark/silly
        # humour), per the user request — not just the common crowd emoji.
        pool = {strip_variation(e) for e in _blend_pool(LAUGH, _laugh_comments())}
        related = {strip_variation(e) for e in RELATED_EMOJI[Emotion.LAUGH]}
        assert pool & related                       # at least one related emoji
        assert strip_variation(EYES) in pool        # and the common crowd emoji

    def test_deterministic(self):
        a = diversify_reply(LAUGH, "favourite_match", _laugh_comments("x"),
                            PROFILE, SET, [])
        b = diversify_reply(LAUGH, "favourite_match", _laugh_comments("x"),
                            PROFILE, SET, [])
        assert a == b

    def test_blend_pool_needs_two_uses(self):
        # a one-off emoji is NOT blended in (needs >= 2)
        rare = _comments(FIRE, FIRE, FIRE, "🥶 once")
        assert strip_variation("🥶") not in {
            strip_variation(e) for e in _blend_pool(FIRE, rare)}

    def test_blend_pool_prefers_real_crowd_mix_over_generic_preset(self):
        # regression: a fire-dominant reel whose crowd ALSO uses laughing a
        # lot used to blend in the generic FIRE-family preset (🐐/💯, "fire
        # fire goat") even when the actual comments were mixing fire+laugh —
        # because the crowd-mix candidate was silently dropped whenever it
        # was also one of the user's favourites (LAUGH is, per PROFILE). The
        # real crowd emoji must now win and rank ahead of the generic preset.
        mixed = _comments(*([f"{FIRE} lit"] * 6 + [f"{LAUGH} dead"] * 5))
        pool = [strip_variation(e) for e in _blend_pool(FIRE, mixed)]
        assert strip_variation(LAUGH) in pool
        goat, hundred = strip_variation("🐐"), strip_variation("💯")
        for generic in (goat, hundred):
            if generic in pool:
                assert pool.index(strip_variation(LAUGH)) < pool.index(generic)

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
