"""Core data structures shared across every module.

These are deliberately plain, serializable dataclasses. The whole "brain" of
the app (rules + reaction engine) consumes and produces these objects, which
means it can be exercised and unit-tested without ever touching a phone.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# --------------------------------------------------------------------------
# Canonical emotions
# --------------------------------------------------------------------------
# Everything (emojis + slang) is normalized down to one of these buckets.
# Each bucket has a representative emoji used when we need to *produce* output.

class Emotion:
    DEAD = "DEAD"        # 💀  "I'm dead", "deceased", "bury me"
    CRYING = "CRYING"    # 😭  "crying", "tears", "sobbing"
    LAUGH = "LAUGH"      # 😂  "lol", "lmao", "hilarious"
    LOVE = "LOVE"        # ❤️  "love this", "cute", "wholesome"
    FIRE = "FIRE"        # 🔥  "goes hard", "banger", "W"
    SHOCK = "SHOCK"      # 😱  "no way", "wtf", "insane"
    ANGRY = "ANGRY"      # 😤  "mad", "rage"

    ALL = (DEAD, CRYING, LAUGH, LOVE, FIRE, SHOCK, ANGRY)


# Representative emoji for each canonical emotion (used to build replies).
CANON_EMOJI = {
    Emotion.DEAD: "💀",
    Emotion.CRYING: "😭",
    Emotion.LAUGH: "😂",
    Emotion.LOVE: "❤️",
    Emotion.FIRE: "🔥",
    Emotion.SHOCK: "😱",
    Emotion.ANGRY: "😤",
}


# --------------------------------------------------------------------------
# User profile + tunable settings
# --------------------------------------------------------------------------

class ReplyStyle:
    SINGLE = "single"          # 💀
    DOUBLE = "double"          # 💀💀
    TEXT_EMOJI = "text_emoji"  # bro 💀
    ALL = (SINGLE, DOUBLE, TEXT_EMOJI)


@dataclass
class Profile:
    """Everything that makes the output sound like *you*."""

    # Ordered most-preferred first. May include emojis outside the canon set.
    emoji_prefs: list[str] = field(default_factory=list)
    # Things you actually type: "bro 💀", "nah 😭", "LMAOO", "😭", "💀"
    common_replies: list[str] = field(default_factory=list)
    reply_style: str = ReplyStyle.SINGLE
    # Optional: extra slang -> canonical Emotion, e.g. {"finished": "DEAD"}
    extra_slang: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            emoji_prefs=list(d.get("emoji_prefs", [])),
            common_replies=list(d.get("common_replies", [])),
            reply_style=d.get("reply_style", ReplyStyle.SINGLE),
            extra_slang={k: v for k, v in d.get("extra_slang", {}).items()},
        )


@dataclass
class Settings:
    """Deterministic knobs. Defaults chosen to match the spec's examples."""

    min_comments: int = 20            # Rule 2 hard gate
    comments_to_read: int = 50        # how many to scrape per reel
    public_weight: float = 0.4        # spec: public_score * 0.4
    personal_weight: float = 0.6      # spec: my_preference * 0.6
    auto_reply_min_confidence: float = 0.6
    min_top_share: float = 0.40       # winner must own >= this share of public signal
    min_margin: float = 0.12          # winner must beat runner-up by >= this
    like_weight_mode: str = "log1p"   # "log1p" | "linear" | "none"
    per_comment_cap: int = 3          # max times one comment can vote for one emotion
    # confidence = a*top_share + b*volume + c*margin_norm
    conf_w_top_share: float = 0.60
    conf_w_volume: float = 0.25
    conf_w_margin: float = 0.15
    volume_full_at: int = 40          # comment count at which "volume" saturates
    margin_full_at: float = 0.30      # margin at which the margin term saturates

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        base = cls()
        for k, v in (d or {}).items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base


# --------------------------------------------------------------------------
# Reel inputs / analysis outputs
# --------------------------------------------------------------------------

@dataclass
class Comment:
    text: str
    likes: int = 0


@dataclass
class ReelContext:
    """Everything the decision layer needs about a single received reel.

    This is the seam between the (device-specific) automation layer and the
    (device-agnostic) decision layer. Whether it comes from a real phone or a
    JSON fixture, the decision logic is identical.
    """

    chat_name: str
    reel_id: str = ""                       # stable-ish handle for logging
    has_preceding_text: bool = False        # Rule 1
    preceding_text: Optional[str] = None
    comments: Optional[list[Comment]] = None  # None => could not read (error)
    comment_count: Optional[int] = None     # reported total (may exceed len(comments))
    read_error: bool = False

    def effective_count(self) -> int:
        if self.comment_count is not None:
            return self.comment_count
        return len(self.comments) if self.comments else 0


# --------------------------------------------------------------------------
# Flags + decisions
# --------------------------------------------------------------------------

class FlagKind:
    CONTEXT_TEXT = "context_text"
    TOO_FEW_COMMENTS = "too_few_comments"
    NO_CONSENSUS = "no_consensus"
    UNABLE_TO_READ = "unable_to_read"
    LOW_CONFIDENCE = "low_confidence"
    NAV_FAILED = "nav_failed"


@dataclass
class Flag:
    kind: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreBreakdown:
    """Full explainability payload for a decision."""

    public_raw: dict[str, float] = field(default_factory=dict)
    public_norm: dict[str, float] = field(default_factory=dict)
    personal_norm: dict[str, float] = field(default_factory=dict)
    final: dict[str, float] = field(default_factory=dict)
    top_share: float = 0.0
    margin: float = 0.0
    n_comments: int = 0
    winner: Optional[str] = None
    public_winner: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class Action:
    AUTO_REPLY = "auto_reply"
    FLAG = "flag"


@dataclass
class Decision:
    action: str
    reply_text: Optional[str] = None
    winning_emotion: Optional[str] = None
    confidence: float = 0.0
    flag: Optional[Flag] = None
    breakdown: Optional[ScoreBreakdown] = None
    chat_name: str = ""
    reel_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------
# Run summary
# --------------------------------------------------------------------------

@dataclass
class RunSummary:
    auto_replied: list[Decision] = field(default_factory=list)
    flagged: list[Decision] = field(default_factory=list)

    def add(self, decision: Decision) -> None:
        if decision.action == Action.AUTO_REPLY:
            self.auto_replied.append(decision)
        else:
            self.flagged.append(decision)

    def counts_by_flag(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.flagged:
            if d.flag:
                out[d.flag.kind] = out.get(d.flag.kind, 0) + 1
        return out
