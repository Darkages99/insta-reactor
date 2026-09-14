"""Deterministic content-safety gate — a net that does NOT trust the LLM.

The reaction model is told (in llm_reply.py) never to join in on cruel or bigoted
reels, but small models ignore that and happily reply "lmao" to a bullying clip.
Auto-sending a laughing/hyping reaction on content that mocks or attacks a person
or group is the worst failure this bot can have, so we guard it deterministically:
if a reel's caption/comments carry clear harmful markers, `harmful_reason` returns
a short reason and the engine FLAGS it for a human instead of ever reacting.

This is a conservative safety net, not a classifier. It fails toward human review
(a false positive just means you handle one reel yourself), and deliberately uses
phrase-level patterns to avoid flagging ordinary hype slang ("he cooked him",
"this goes crazy"). It never raises.
"""

from __future__ import annotations

import re

# Phrase-level patterns for fairly unambiguous harmful content. Kept specific so
# normal reels (sports "cooked him", "insane", roast-your-friend banter) don't
# trip it. Each entry: (compiled regex, short reason).
_PATTERNS = [
    (re.compile(r"\bk+ys+\b|kill\s+your\s?self|\bkys\b", re.I), "self-harm encouragement"),
    (re.compile(r"made\s+(her|him|them)\s+cry|we\s+made\s+\w+\s+cry", re.I), "mocking someone's distress"),
    (re.compile(r"\bget\s+(rekt|wrecked)\b", re.I), "pile-on / harassment"),
    (re.compile(r"\b(she|he|they)\s+deserved\s+it\b", re.I), "endorsing harm to a person"),
    (re.compile(r"\bwhat\s+a\s+loser\b|\byou'?re\s+(a\s+)?(loser|pathetic|worthless)\b"
                r"|\b(worthless|pathetic)\s+(loser|human|person)\b", re.I), "demeaning a person"),
    (re.compile(r"\bnobody\s+(likes|loves)\s+(you|her|him|them)\b", re.I), "targeted cruelty"),
    (re.compile(r"ranking\s+.*\b(race|races|religion|gender|genders|people|group|groups|ethnic\w*)\b"
                r".*\b(worst|best)\b|\bworst\s+to\s+best\b.*\b(people|group|groups|race|races)\b", re.I),
     "ranking/dehumanising a group"),
    (re.compile(r"\b(go\s+back\s+to\s+your\s+country|subhuman|less\s+than\s+human|don'?t\s+deserve\s+to\s+(live|exist))\b", re.I),
     "dehumanising language"),
]


def harmful_reason(caption: str, comments) -> str | None:
    """Return a short reason if the reel looks cruel/bigoted/harassing, else None.

    `comments` is an iterable of objects with a `.text` (or plain strings). Scans
    the caption plus the comment texts. Conservative by design; never raises.
    """
    try:
        parts = [caption or ""]
        for c in comments or []:
            parts.append(getattr(c, "text", c) or "")
        blob = "  ".join(parts)
        for rx, reason in _PATTERNS:
            if rx.search(blob):
                return reason
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# Tone guard — a second deterministic net that does NOT trust the LLM.
#
# Small models occasionally reply "lmao"/🔥 to a wholesome reunion, a grief
# post, or an educational explainer even though the prompt forbids it. Sending a
# laughing/hyping reaction on heartfelt or serious content reads as tone-deaf and
# is a bad enough failure that we catch it deterministically: if the LLM's reply
# is a laugh/hype reaction but the reel's own signals (caption + crowd) are
# clearly wholesome/somber/educational with NO laugh/hype support from the crowd,
# `tone_conflict` returns a short reason and the runner hands the reel to a human
# instead of sending the mismatched reaction.
#
# Deliberately narrow to avoid false positives: it only fires when the crowd
# itself is NOT laughing/hyping (a genuinely funny reel has a laugh-heavy crowd,
# so the guard stays out of its way). Never raises.
# --------------------------------------------------------------------------

_LAUGH = re.compile(
    r"💀|😂|🤣|😹|☠️|⚰️|\blmf?ao+\b|\blol(?:ol)?\b|\bha(?:ha)+\b|"
    r"\bhilarious\b|\bim\s+dead\b|\bi'?m\s+dead\b|\bdead\b|\bdyin[g']?\b",
    re.I)
_HYPE = re.compile(
    r"🔥|🥶|\bgoes\s+(?:crazy|hard)\b|\bcooked\b|\bsheesh\b|\bbanger\b|"
    r"\bthis\s+slaps\b|\bslaps\b",
    re.I)
_WARMTH = re.compile(
    r"🥹|🥺|❤️|😍|🫶|🕊️|💔|\bwholesome\b|\bprecious\b|\badorable\b|\bso\s+cute\b|"
    r"\bso\s+sweet\b|\bbeautiful\b|\bmelting\b|\bnot\s+crying\b|\breunion\b|"
    r"\bfirst\s+steps?\b|\bsending\s+(?:love|strength)\b|\bso\s+sorry\b|"
    r"\brest\s+easy\b|\bpassed\s+away\b|\bloss\b|\bgrief\b|\bheartbreaking\b|"
    r"\bdevastating\b|\bpraying\b|🙏",
    re.I)
_EDU = re.compile(
    r"\bTIL\b|\binformative\b|\bfascinating\b|\bexplainer\b|\bexplanation\b|"
    r"\bwell\s+explained\b|\bwell\s+researched\b|\blearned\s+(?:a\s+lot|something)\b|"
    r"\bnever\s+knew\b|\bunderrated\s+history\b|🧠",
    re.I)


def _blob(caption: str, comments) -> str:
    parts = [caption or ""]
    for c in comments or []:
        parts.append(getattr(c, "text", c) or "")
    return "  ".join(parts)


def tone_conflict(caption: str, comments, reply: str) -> str | None:
    """Return a short reason if `reply` clashes with the reel's tone, else None.

    Only guards laugh/hype replies, and only when the crowd is NOT itself
    laughing/hyping (so genuinely funny/hype reels are untouched):
      * laugh OR hype reply on clearly WARM/SOMBER content  -> conflict
      * laugh reply on clearly EDUCATIONAL content           -> conflict
        (🔥="cool fact" is fine on educational, so hype is allowed there)
    """
    try:
        reply = reply or ""
        reply_laugh = bool(_LAUGH.search(reply))
        reply_hype = bool(_HYPE.search(reply))
        if not (reply_laugh or reply_hype):
            return None

        blob = _blob(caption, comments)
        crowd_laugh = bool(_LAUGH.search(blob))
        crowd_hype = bool(_HYPE.search(blob))

        if _WARMTH.search(blob) and not (crowd_laugh or crowd_hype):
            if reply_laugh:
                return "laughing at heartfelt/somber content"
            if reply_hype:
                return "hyping heartfelt/somber content"
        if _EDU.search(blob) and reply_laugh and not crowd_laugh:
            return "laughing at educational content"
    except Exception:
        return None
    return None
