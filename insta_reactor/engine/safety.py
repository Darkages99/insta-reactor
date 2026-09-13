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
