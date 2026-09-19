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
    # Short phrases you've explicitly OK'd for the bot to reuse verbatim on any
    # reel, on top of bare emoji reactions. Everything else you've ever typed
    # is too contextual to safely generalize, so it's excluded from training
    # (see engine.normalize.is_trainable_reply). Hand-edited in config.json.
    approved_phrases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            emoji_prefs=list(d.get("emoji_prefs", [])),
            common_replies=list(d.get("common_replies", [])),
            reply_style=d.get("reply_style", ReplyStyle.SINGLE),
            extra_slang={k: v for k, v in d.get("extra_slang", {}).items()},
            approved_phrases=list(d.get("approved_phrases", [])),
        )


@dataclass
class Settings:
    """Deterministic knobs. Defaults chosen to match the spec's examples."""

    min_comments: int = 5             # Rule 2 hard gate
    comments_to_read: int = 50        # how many to scrape per reel
    # Only the newest N received reels (counting from the bottom of the thread)
    # are considered "new/unread" and processed per run. Instagram exposes no
    # reliable per-message read flag, so this newest-N cap is the practical
    # proxy for "unread". Sized to clear a full spam burst (people dumping
    # 10-20 reels at once) in one run; the browser backend's reacted watermark
    # (browser/watermark.py) makes raising this safe — it stops reprocessing
    # reels a prior run already handled, so this is purely a per-run ceiling,
    # not a "how much history to rescan" knob.
    max_new_reels: int = 25
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

    # --- offline ML ensemble ------------------------------------------------
    # The rule/emoji dictionary always votes; when the model is enabled its
    # per-comment distribution is blended in with this weight (0 => rules only,
    # identical to the pre-ML behaviour). The meme buckets DEAD/FIRE are still
    # dominated by the rules because the general model has no class for them.
    use_model: bool = False           # attempt to load the offline classifier
    model_name: str = "SamLowe/roberta-base-go_emotions"
    # 0..1 share given to the model per comment. Kept < 0.5 so the rules keep
    # the edge on meme buckets (DEAD/FIRE) the model has no class for; raise it
    # toward 1.0 to trust the model more on ambiguous free-text comments.
    ensemble_model_weight: float = 0.4

    # --- remote LLM (OpenAI-compatible, e.g. OpenRouter) --------------------
    # Off by default => fully deterministic + private, the original behaviour.
    # When on, the LLM is consulted ONLY for reels the deterministic engine
    # would otherwise flag as no_consensus/low_confidence (llm_mode="assist"),
    # so cost stays proportional to the ambiguous tail, not every reel. It can
    # only ever UPGRADE a flag into an in-style auto-reply — never override a
    # confident deterministic decision. The API key never lives here; it comes
    # from env/data/secrets.json (see secrets.py). Set "always" to run it on
    # every auto-reply candidate, "off" to disable without clearing use_llm.
    use_llm: bool = False
    llm_mode: str = "assist"                      # off | assist | always
    llm_model: str = "openai/gpt-4o-mini"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout: float = 20.0
    llm_max_tokens: int = 120
    llm_max_reply_len: int = 40       # reject model replies longer than this
    llm_min_confidence: float = 0.55  # below this, keep the human flag
    # Reaction sampling temperature. Kept moderate: high temps make the model
    # occasionally pick an off-tone reaction (a stray "lmao" on a wholesome/
    # somber reel); cross-reel variety is added separately by diversify_reply.
    llm_temperature: float = 0.5
    rag_top_k: int = 5                # past examples fed to the LLM as few-shot
    log_interactions: bool = True     # append decisions to the interaction log
    log_raw_text: bool = False        # also store raw comment text (debug/opt-in)

    # --- reply selection ----------------------------------------------------
    # "Very popular" comments can be echoed back verbatim as the reaction.
    popular_min_likes: int = 50       # abs. like floor to qualify as "very popular"
    popular_like_share: float = 0.50  # or owns >= this share of all read likes
    # Kept short on purpose: a short comment ("LMAOO 💀💀") is a generic reaction
    # that reads naturally coming from you. A long one is usually a specific
    # reference/question/take on the video's content (or spam), and echoing it
    # verbatim as if it's your own reaction is often wrong. The longer the
    # comment, the less likely it's a safe, generic echo — so this stays tight.
    max_verbatim_len: int = 24        # never echo a comment longer than this
    prefer_favourite_emoji: bool = True  # bias reply toward your favourites seen in comments

    # --- personal echo bypass ------------------------------------------------
    # If your own preferred emoji/common-reply literally shows up this many
    # times (or more) among the comments — regardless of whether the wider
    # crowd agrees with each other — that's strong enough direct evidence on
    # its own to skip the crowd-consensus gate (min_top_share/min_margin).
    # The confidence gate still applies as a floor.
    personal_echo_min_matches: int = 2

    # --- native reel reaction -------------------------------------------------
    # When on, single-emoji replies are first attempted via IG's real long-press
    # reaction sheet (Navigator.react_to_reel_in_viewer) instead of being typed
    # into the reply composer. Falls back to the typed reply automatically if
    # the native gesture fails, so this is safe to leave on. Multi-emoji and
    # text+emoji replies always use the typed path (the sheet is single-pick).
    native_reaction_enabled: bool = True

    # --- browser backend (Instagram web via Playwright) ----------------------
    # Run the automation browser without a visible window. Keep False for the
    # first run so the user can log into Instagram once in the opened window.
    browser_headless: bool = False

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
    has_following_text: bool = False        # Rule 1b: sender's own follow-up
    following_text: Optional[str] = None
    comments: Optional[list[Comment]] = None  # None => could not read (error)
    comment_count: Optional[int] = None     # reported total (may exceed len(comments))
    read_error: bool = False
    # The reel's own caption (poster's text) — the single strongest signal of
    # what the reel is actually ABOUT. Fed to the LLM so it reacts to the content
    # itself, not just the crowd's (often hype) comments. Empty/None when the
    # backend couldn't read it; the pipeline degrades to comments-only.
    caption: Optional[str] = None
    # Filesystem path to a screenshot of the reel's thumbnail, captured by the
    # browser backend. Lets the UI show a *picture* of any un-reacted reel
    # instead of an opaque "reel #2". None for backends that don't capture one.
    thumbnail_path: Optional[str] = None
    # 1-based position of this reel counting from the BOTTOM of the thread as a
    # human scrolling their DM would count them (1 = most recent reel bubble).
    # This is how the review UI tells you *where to look* to find an un-reacted
    # reel and handle it yourself. None when the backend can't determine it.
    position_from_bottom: Optional[int] = None

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
    # A plain text message in the chat (not a reel). The bot only reacts to
    # reels, so any incoming text — even a single one — is surfaced to you.
    INCOMING_TEXT = "incoming_text"
    # llm_mode="always": the LLM declined to author a reply (should_reply=false)
    # or failed. We flag for a human rather than fall back to the context-blind
    # deterministic reaction — that fallback is how a hype 'lmao'/🔥 lands on a
    # somber or wholesome reel.
    LLM_DECLINED = "llm_declined"
    # Reel looks cruel/bullying/bigoted/harassing (engine/safety.py). We never
    # auto-react to it — always a human's call — regardless of what the crowd or
    # the LLM would say.
    SENSITIVE_CONTENT = "sensitive_content"
    # You stepped in while the bot was running — it saw you type a message or
    # place your own reaction on a reel (or you hit Stop). The bot backs off the
    # whole run so it never talks over you mid-conversation.
    USER_ACTIVE = "user_active"


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
    # How reply_text was chosen: "popular_verbatim" | "favourite_match" |
    # "emotion" (fallback) | "llm" (AI-synthesised). For explainability/summary.
    reply_source: Optional[str] = None
    # Screenshot of the reel's thumbnail (copied from its ReelContext), so the
    # UI can show un-reacted reels as a gallery of pictures.
    thumbnail_path: Optional[str] = None
    # Where the reel sits in the thread, counted from the bottom (copied from
    # its ReelContext). The review UI shows this as "Nth reel from the bottom"
    # so you can find the un-reacted reel in your DM and handle it.
    position_from_bottom: Optional[int] = None

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
