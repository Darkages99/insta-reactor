"""Scenario eval — grade the bot's *reaction quality* across varied reel types.

Unlike tools/eval_harness.py (which replays your real interaction log), this
runs a hand-built corpus of diverse synthetic reels through the FULL production
decision path — deterministic engine + LLM synthesis/rescue + reply diversify —
exactly as the runner does live, and checks that the reaction *fits the reel*.

The core quality bar (see engine/llm_reply.py's system prompt): react to what
the reel is ABOUT and match its tone. Hype slang / 🔥 / laugh reactions fit a
flex/funny/sports clip but are WRONG on something sad, serious, educational, or
wholesome. Each scenario declares a `tone`; the grader flags tone mismatches
(e.g. a 🔥 or "lmao" landing on a tragedy), empty replies, and comment-parroting.

Run:  python -m insta_reactor.tools.scenario_eval [--json] [--repeat N] [--verbose]
Needs use_llm + a key in data/config.json / secrets to exercise the LLM path;
without a key it still grades the deterministic path.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from ..models import Comment, ReelContext, Action
from ..config import load_config
from ..engine.reaction import decide_reaction
from ..engine.reply_select import diversify_reply
from ..runner import Runner


# --- tone vocabularies the grader keys off ---------------------------------
# LAUGH: reads as laughing-at. Wrong on anything sad, wholesome, OR educational
# (you don't laugh at a funeral, a baby's first steps, or a physics explainer).
LAUGH = [
    "💀", "😂", "🤣", "😹", "☠️", "⚰️",
    "lmao", "lmfao", "lmaoo", "lmaooo", "haha", "hahaha", "lol", "lolol",
    "hilarious", "dead ", "im dead", "bruh moment", "ratio",
]
# HYPE: reads as flex/impressive-hype. Wrong on somber/wholesome (🔥 on a death
# or a baby is tone-deaf), but ACCEPTABLE on educational ("🔥" = "cool fact").
HYPE = ["🔥", "🥶", "goes crazy", "goes hard", "cooked", "sheesh", "banger",
        "this slaps"]
# Back-compat alias for any external reference.
HYPE_LAUGH = LAUGH + HYPE
# Warmth tokens — used only as a soft positive signal for wholesome scenarios.
WARMTH = ["🥹", "😭", "❤️", "🥺", "😍", "🫶", "🙏", "beautiful", "sweet",
          "wholesome", "precious", "adorable", "love this", "so cute"]


def _has_any(text: str, toks) -> str | None:
    low = (text or "").lower()
    for t in toks:
        if t.lower() in low:
            return t
    return None


@dataclass
class Scenario:
    name: str
    tone: str            # hype | funny | somber | wholesome | educational | needs_human
    caption: str
    comments: list[tuple[str, int]]
    note: str = ""

    def ctx(self) -> ReelContext:
        return ReelContext(
            chat_name="ScenarioEval",
            reel_id=self.name,
            caption=self.caption,
            comments=[Comment(text=t, likes=l) for t, l in self.comments],
            comment_count=len(self.comments),
        )


def _c(*texts: str) -> list[tuple[str, int]]:
    """Quick helper: comments with mild descending like counts."""
    return [(t, max(0, 40 - i * 2)) for i, t in enumerate(texts)]


# A crowd blob to pad a scenario past min_comments with on-tone chatter.
def _pad(tone_words: list[str], n: int = 14) -> list[tuple[str, int]]:
    out = []
    for i in range(n):
        out.append((tone_words[i % len(tone_words)], 30 - i))
    return out


SCENARIOS: list[Scenario] = [
    # --- clearly hype: hype reactions are correct --------------------------
    Scenario("flex_dunk", "hype",
             "posterized him at the buzzer 😤🏀",
             _c("BROO", "he JUMPED", "insane hops", "🔥🔥🔥", "cooked him")
             + _pad(["nah this crazy", "🔥", "goes hard", "W", "insane"])),
    Scenario("guitar_shred", "hype",
             "learned this solo in a week",
             _c("this goes crazy", "so clean", "🔥", "talented", "how??")
             + _pad(["insane", "🔥", "so good", "goes hard", "W"])),

    # --- funny: laugh reactions are correct --------------------------------
    Scenario("cat_fail", "funny",
             "he really thought he could make the jump 😹",
             _c("LMAOO", "not the faceplant", "😂😂", "im crying", "sirr")
             + _pad(["lmao", "😂", "dead", "so funny", "hahaha"])),

    # --- somber: hype/laugh reactions are WRONG ----------------------------
    Scenario("pet_loss", "somber",
             "saying goodbye to my best friend of 15 years. rest easy buddy 🕊️",
             _c("so sorry for your loss", "this made me cry", "🕊️❤️",
                "sending love", "rest easy")
             + _pad(["so sorry", "🥺", "sending love", "💔", "hugs"])),
    Scenario("mental_health", "somber",
             "it's been the hardest year of my life. please check on your friends.",
             _c("thank you for sharing this", "you're so brave", "here for you",
                "this is important", "sending strength")
             + _pad(["stay strong", "🙏", "proud of you", "here for you", "❤️"])),
    Scenario("disaster_news", "somber",
             "entire neighbourhood lost to the floods overnight.",
             _c("praying for them", "heartbreaking", "how can we help",
                "this is devastating", "🙏")
             + _pad(["so sad", "praying", "donate here", "heartbreaking", "😢"])),

    # --- wholesome: warm reactions fit, hype/laugh do not ------------------
    Scenario("baby_firststeps", "wholesome",
             "her very first steps 🥹",
             _c("the CUTEST", "🥹🥹", "melting", "precious", "aww")
             + _pad(["so cute", "🥹", "adorable", "sweet", "precious"])),
    Scenario("soldier_reunion", "wholesome",
             "surprised my little brother after 2 years deployed",
             _c("i'm not crying you are", "so wholesome", "🥹❤️",
                "this is beautiful", "best feeling")
             + _pad(["🥹", "beautiful", "so sweet", "love this", "❤️"])),

    # --- educational: thoughtful fits, hype slang does not -----------------
    Scenario("science_fact", "educational",
             "why the sky is actually violet, not blue — a 60s explainer",
             _c("TIL", "this is fascinating", "never knew this",
                "great explanation", "saving this")
             + _pad(["interesting", "TIL", "well explained", "learned a lot",
                     "👏"])),
    Scenario("history_thread", "educational",
             "the 1400s trade route almost nobody talks about",
             _c("underrated history", "so informative", "more of this",
                "well researched", "fascinating")
             + _pad(["informative", "TIL", "great video", "learned something",
                     "🧠"])),

    # --- needs human: personal question / DM-bait -> expect NO auto reply --
    Scenario("personal_question", "needs_human",
             "should i text him back? be honest 😭 what would you do",
             _c("no don't", "depends", "block him", "we need context",
                "call him instead")
             + _pad(["no", "yes", "depends", "don't", "context?"])),

    # --- adversarial: crowd is hype but CONTENT is somber ------------------
    Scenario("somber_hype_crowd", "somber",
             "the last video i ever took of my grandma before she passed.",
             _c("🔥🔥", "goes hard", "W", "this slaps", "banger")  # trap crowd
             + _pad(["🔥", "goes hard", "W", "fire", "banger"]),
             note="crowd says hype; content is a death — must follow content"),

    # --- ambiguous crowd: no clear consensus (tests rescue path) -----------
    Scenario("mixed_bag", "funny",
             "rating street food i tried in bangkok",
             _c("looks so good", "😂 the reaction", "im hungry now",
                "which stall", "yum")
             + _pad(["😋", "yum", "hungry", "looks good", "😂"])),

    # --- foreign language caption: still react to vibe ---------------------
    Scenario("foreign_wholesome", "wholesome",
             "मेरी माँ ने पहली बार समुंदर देखा 🥹",  # "my mom saw the ocean for the first time"
             _c("so beautiful", "🥹", "her smile", "precious moment", "love this")
             + _pad(["🥹", "beautiful", "so sweet", "precious", "❤️"])),

    # --- low comment count: deterministic gate should flag too_few --------
    Scenario("too_few", "hype",
             "quick trick shot",
             _c("nice", "clean", "🔥"),   # only 3 comments < min_comments
             note="expected: flagged too_few_comments (not enough signal)"),
]


@dataclass
class Result:
    name: str
    tone: str
    action: str
    reply: str
    source: str
    ok: bool
    why: str = ""


def _grade(sc: Scenario, decision) -> Result:
    action = decision.action
    reply = decision.reply_text or ""
    source = decision.reply_source or ("flag:" + (decision.flag.kind if decision.flag else "?"))

    # needs_human: flagging is ideal, but a pure-emoji reaction (e.g. 😭 to a
    # relatable dilemma) is acceptable degradation. What's NOT acceptable is the
    # bot typing a WORDED answer — attempting to give advice a human should give.
    if sc.tone == "needs_human":
        if action != Action.AUTO_REPLY:
            return Result(sc.name, sc.tone, action, reply, source, True,
                          "flagged for a human (ideal)")
        worded = any(ch.isalpha() for ch in reply)
        ok = not worded
        return Result(sc.name, sc.tone, action, reply, source, ok,
                      "emoji-only reaction (acceptable)" if ok
                      else "typed a worded answer to a reel that needs a human")
    if sc.name == "too_few":
        ok = action != Action.AUTO_REPLY
        return Result(sc.name, sc.tone, action, reply, source, ok,
                      "" if ok else "auto-replied despite < min_comments")

    # everything else: if it auto-replied, the reply must FIT the tone.
    if action != Action.AUTO_REPLY:
        # Flagging is acceptable (handed to a human) but not the goal — note it.
        return Result(sc.name, sc.tone, action, reply, source, True,
                      "flagged (no reaction sent — acceptable but not ideal)")
    if not reply.strip():
        return Result(sc.name, sc.tone, action, reply, source, False, "empty reply")

    # somber/wholesome: neither laughing NOR flex-hype fits. educational: no
    # laughing (odd on an explainer), but 🔥 = "cool fact" is fine.
    forbidden = LAUGH if sc.tone == "educational" else (LAUGH + HYPE)
    if sc.tone in ("somber", "wholesome", "educational"):
        bad = _has_any(reply, forbidden)
        if bad:
            return Result(sc.name, sc.tone, action, reply, source, False,
                          f"tone mismatch: {bad!r} on a {sc.tone} reel")
    return Result(sc.name, sc.tone, action, reply, source, True, "fits tone")


def run(repeat: int = 1):
    cfg = load_config()
    runner = Runner(backend=None, config=cfg)   # backend unused for decisions
    results: list[Result] = []
    for sc in SCENARIOS:
        worst = None
        for _ in range(max(1, repeat)):
            ctx = sc.ctx()
            decision = decide_reaction(ctx, cfg.profile, cfg.settings, model=runner.model)
            decision = runner._maybe_llm(ctx, decision)
            if decision.action == Action.AUTO_REPLY:
                decision.reply_text = diversify_reply(
                    decision.reply_text, decision.reply_source or "", ctx.comments,
                    cfg.profile, cfg.settings, runner._recent_replies)
                runner._recent_replies.append(decision.reply_text)
            r = _grade(sc, decision)
            # keep the worst (a failure on any repeat is a failure)
            if worst is None or (worst.ok and not r.ok):
                worst = r
        results.append(worst)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scenario_eval")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each scenario N times; a failure on any counts (catches flaky tone)")
    ap.add_argument("--verbose", action="store_true", help="show every reply")
    args = ap.parse_args(argv)

    results = run(args.repeat)
    n_fail = sum(1 for r in results if not r.ok)
    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False))
        return 1 if n_fail else 0

    print("Scenario eval — reaction quality across reel types")
    print("=" * 66)
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        line = f"[{mark}] {r.name:20s} {r.tone:11s} -> {r.action:11s} {r.reply!r}"
        print(line)
        if (not r.ok or args.verbose) and r.why:
            print(f"         └─ {r.why}  (source={r.source})")
    print("-" * 66)
    print(f"{len(results)-n_fail}/{len(results)} passed, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
