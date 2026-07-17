"""Static data: emoji -> emotion and slang -> emotion mappings.

Kept as plain data so it is trivial to audit, extend, and diff. This is the
single place to add new slang without touching any logic.
"""

from __future__ import annotations

from ..models import Emotion


# --------------------------------------------------------------------------
# Emoji -> Emotion
# --------------------------------------------------------------------------
# Keyed by the *base* codepoint (variation selector U+FE0F stripped before
# lookup). Unknown emojis are handled by the engine as their own emotion so a
# user's niche favorite still survives.

EMOJI_EMOTION: dict[str, str] = {
    # DEAD / skull
    "💀": Emotion.DEAD,
    "☠": Emotion.DEAD,
    # CRYING
    "😭": Emotion.CRYING,
    "😢": Emotion.CRYING,
    "😥": Emotion.CRYING,
    "😪": Emotion.CRYING,
    "🥲": Emotion.CRYING,
    # LAUGH
    "😂": Emotion.LAUGH,
    "🤣": Emotion.LAUGH,
    "😹": Emotion.LAUGH,
    # LOVE
    "❤": Emotion.LOVE,
    "🧡": Emotion.LOVE,
    "💛": Emotion.LOVE,
    "💚": Emotion.LOVE,
    "💙": Emotion.LOVE,
    "💜": Emotion.LOVE,
    "🖤": Emotion.LOVE,
    "🤍": Emotion.LOVE,
    "🤎": Emotion.LOVE,
    "💗": Emotion.LOVE,
    "💓": Emotion.LOVE,
    "💞": Emotion.LOVE,
    "💕": Emotion.LOVE,
    "😍": Emotion.LOVE,
    "🥰": Emotion.LOVE,
    "😘": Emotion.LOVE,
    # FIRE
    "🔥": Emotion.FIRE,
    "💯": Emotion.FIRE,
    "🐐": Emotion.FIRE,   # GOAT
    # SHOCK
    "😱": Emotion.SHOCK,
    "😨": Emotion.SHOCK,
    "😧": Emotion.SHOCK,
    "😮": Emotion.SHOCK,
    "😲": Emotion.SHOCK,
    "🤯": Emotion.SHOCK,
    # ANGRY
    "😤": Emotion.ANGRY,
    "😡": Emotion.ANGRY,
    "🤬": Emotion.ANGRY,
    "😠": Emotion.ANGRY,
}


# --------------------------------------------------------------------------
# Single-word / token slang -> Emotion
# --------------------------------------------------------------------------
# Matched against whitespace/punctuation-delimited tokens after elongation is
# collapsed ("deaad" -> "dead", "sooo" -> "so"). Keep these as ROOT forms.

WORD_SLANG: dict[str, str] = {
    # DEAD
    "dead": Emotion.DEAD,
    "ded": Emotion.DEAD,
    "deceased": Emotion.DEAD,
    "rip": Emotion.DEAD,
    "dying": Emotion.DEAD,
    "died": Emotion.DEAD,
    "gone": Emotion.DEAD,
    "finished": Emotion.DEAD,
    # CRYING
    "crying": Emotion.CRYING,
    "cryin": Emotion.CRYING,
    "cry": Emotion.CRYING,
    "tears": Emotion.CRYING,
    "sobbing": Emotion.CRYING,
    "sobbin": Emotion.CRYING,
    "bawling": Emotion.CRYING,
    "weeping": Emotion.CRYING,
    # LAUGH (the messy laughter family is also handled by regex in normalize.py)
    "hilarious": Emotion.LAUGH,
    "funny": Emotion.LAUGH,
    "weak": Emotion.LAUGH,
    "wheezing": Emotion.LAUGH,
    "wheeze": Emotion.LAUGH,
    "rofl": Emotion.LAUGH,
    # LOVE
    "adorable": Emotion.LOVE,
    "cute": Emotion.LOVE,
    "wholesome": Emotion.LOVE,
    "precious": Emotion.LOVE,
    "aww": Emotion.LOVE,
    "aw": Emotion.LOVE,
    "awww": Emotion.LOVE,
    # FIRE
    "fire": Emotion.FIRE,
    "hard": Emotion.FIRE,
    "banger": Emotion.FIRE,
    "slaps": Emotion.FIRE,
    "cold": Emotion.FIRE,
    "snapped": Emotion.FIRE,
    "sheesh": Emotion.FIRE,
    "goated": Emotion.FIRE,
    "w": Emotion.FIRE,
    "dub": Emotion.FIRE,
    # SHOCK
    "wtf": Emotion.SHOCK,
    "omg": Emotion.SHOCK,
    "insane": Emotion.SHOCK,
    "crazy": Emotion.SHOCK,
    "unreal": Emotion.SHOCK,
    "wild": Emotion.SHOCK,
    "wtaf": Emotion.SHOCK,
    # ANGRY
    "mad": Emotion.ANGRY,
    "angry": Emotion.ANGRY,
    "rage": Emotion.ANGRY,
    "furious": Emotion.ANGRY,
    "pissed": Emotion.ANGRY,
}


# --------------------------------------------------------------------------
# Multi-word phrase slang -> Emotion
# --------------------------------------------------------------------------
# Matched as substrings (word-boundaried) on the cleaned comment text, longest
# first so "goes hard" wins before "hard".

PHRASE_SLANG: dict[str, str] = {
    "bury me": Emotion.DEAD,
    "im dead": Emotion.DEAD,
    "i am dead": Emotion.DEAD,
    "im deceased": Emotion.DEAD,
    "im gone": Emotion.DEAD,
    "killed me": Emotion.DEAD,
    "this killed me": Emotion.DEAD,
    "six feet under": Emotion.DEAD,
    "6 feet under": Emotion.DEAD,
    "not me": Emotion.DEAD,           # "not me doing X" self-drag, usually 💀

    "im crying": Emotion.CRYING,
    "i am crying": Emotion.CRYING,
    "tearing up": Emotion.CRYING,
    "in tears": Emotion.CRYING,

    "cant breathe": Emotion.LAUGH,
    "i cant breathe": Emotion.LAUGH,
    "im weak": Emotion.LAUGH,
    "im wheezing": Emotion.LAUGH,
    "so funny": Emotion.LAUGH,

    "love this": Emotion.LOVE,
    "love it": Emotion.LOVE,
    "my heart": Emotion.LOVE,
    "so cute": Emotion.LOVE,
    "too cute": Emotion.LOVE,

    "goes hard": Emotion.FIRE,
    "this goes hard": Emotion.FIRE,
    "hard af": Emotion.FIRE,
    "this slaps": Emotion.FIRE,

    "no way": Emotion.SHOCK,
    "bro what": Emotion.SHOCK,
    "what the": Emotion.SHOCK,
    "how is this": Emotion.SHOCK,
}
