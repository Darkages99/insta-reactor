"""Turn a raw comment string into a bag of weighted emotion signals.

Pure functions, no third-party deps. This is the layer that maps the messy
reality of internet comments ("LMAOOO 💀💀 bro im deceased") onto the small set
of canonical emotions.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from ..models import Emotion
from .slang_map import EMOJI_EMOTION, WORD_SLANG, PHRASE_SLANG


# Emoji-ish codepoints. Deliberately broad; we only *use* matches that either
# land in EMOJI_EMOTION or are counted as a raw-emoji fallback.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols & pictographs (+ supplemental / extended-A)
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U00002B00-\U00002BFF"   # misc symbols and arrows (stars, etc.)
    "\U0001F000-\U0001F0FF"   # mahjong/dominoes/cards
    "\U00002190-\U000021FF"   # arrows (rare in reactions, harmless)
    "]"
)

_VS16 = "️"   # emoji variation selector
_ZWJ = "‍"    # zero-width joiner

# Laughter is too elongated/variable for a dict; match its whole family here.
_LAUGH_RE = re.compile(
    r"(?:\bl+m+f?a+o+\b"      # lmao, lmfao, lmaooo, lmfaooo
    r"|\blo+l+z?\b"           # lol, loll, lolz, looool
    r"|\brofl(?:mao)?\b"      # rofl, roflmao
    r"|\b(?:ah|ha){2,}h?\b"   # haha, hahaha, ahah, ahahah
    r"|\b(?:he){2,}h?\b"      # hehe, hehehe
    r"|\b(?:hi){2,}\b"        # hihi
    r"|\bja(?:ja)+\b)"        # jaja (spanish laugh)
)

# Collapse a run of 3+ identical letters down to 1 for token matching:
# "deaaad" -> "dead", "sooo" -> "so". Runs of only 2 ("madd") are left alone
# to avoid mangling ordinary words ("cool" -> "col").
_RUN_RE = re.compile(r"(.)\1{2,}")


def strip_variation(emoji: str) -> str:
    """Remove VS16 / ZWJ so '❤️' matches the base '❤' key."""
    return emoji.replace(_VS16, "").replace(_ZWJ, "")


def extract_emojis(text: str) -> list[str]:
    """Return each emoji codepoint occurrence (VS16 stripped)."""
    out: list[str] = []
    for ch in text:
        if _EMOJI_RE.match(ch):
            base = strip_variation(ch)
            if base:
                out.append(base)
    return out


def _clean_text(text: str) -> str:
    """Lowercase, drop emojis, turn punctuation into spaces, squeeze runs."""
    text = text.lower()
    text = _EMOJI_RE.sub(" ", text)
    # Normalize unicode punctuation/accents to ascii-ish where possible.
    text = unicodedata.normalize("NFKD", text)
    # Replace anything that isn't a letter/number/space with a space.
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = _RUN_RE.sub(r"\1", text)       # deaaad -> dead, sooo -> so
    text = re.sub(r"\s+", " ", text).strip()
    return text


def signals_for_comment(
    text: str,
    extra_word_slang: dict[str, str] | None = None,
) -> Counter:
    """Map one comment to a Counter of {emotion: raw_count}.

    Counts *occurrences* (an emoji typed 3x counts 3x); per-comment capping is
    applied later by the scorer so spam can't dominate. Unknown emojis are
    counted under their own key (the emoji itself) so a niche favorite still
    has a voice.
    """
    counts: Counter = Counter()

    # 1) Emojis (highest signal / least ambiguous).
    for e in extract_emojis(text):
        emotion = EMOJI_EMOTION.get(e, e)   # unknown emoji -> itself as a key
        counts[emotion] += 1

    cleaned = _clean_text(text)
    if not cleaned:
        return counts

    padded = f" {cleaned} "

    # 2) Laughter family via regex (before generic word matching).
    for _ in _LAUGH_RE.findall(cleaned):
        counts[Emotion.LAUGH] += 1

    # 3) Multi-word phrases, longest first. Each match *consumes* its span so a
    #    phrase and the words inside it are never double-counted: "this goes
    #    hard" must not also fire "goes hard" and "hard". Bounding spaces are
    #    preserved so adjacent phrases still match on their word boundaries.
    for phrase in sorted(PHRASE_SLANG, key=len, reverse=True):
        needle = f" {phrase} "
        while True:
            idx = padded.find(needle)
            if idx == -1:
                break
            counts[PHRASE_SLANG[phrase]] += 1
            s, e = idx + 1, idx + len(needle) - 1
            padded = padded[:s] + (" " * (e - s)) + padded[e:]

    # 4) Single tokens, over whatever the phrases did not consume.
    merged_words = dict(WORD_SLANG)
    if extra_word_slang:
        for k, v in extra_word_slang.items():
            merged_words[_RUN_RE.sub(r"\1", k.lower())] = v
    for tok in padded.split():
        emotion = merged_words.get(tok)
        if emotion:
            counts[emotion] += 1

    return counts
