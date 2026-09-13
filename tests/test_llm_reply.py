"""Prompt building, response parsing, and the safety rails of the LLM layer."""

from __future__ import annotations

from insta_reactor.models import Profile, Settings, Comment, ReelContext, ReplyStyle
from insta_reactor.engine import llm_reply
from insta_reactor.engine.llm_reply import suggest_reply, build_prompt


class FakeClient:
    """A ChatClient stub that returns a canned string (or raises)."""
    def __init__(self, reply=None, raise_it=False):
        self.reply = reply
        self.raise_it = raise_it
        self.calls = []

    def complete(self, system, user, *, temperature=0.7, max_tokens=120):
        self.calls.append((system, user))
        if self.raise_it:
            raise RuntimeError("boom")
        return self.reply


def _ctx(texts):
    return ReelContext(chat_name="x", reel_id="r1",
                       comments=[Comment(t) for t in texts])


def test_redaction_strips_handles_and_links():
    ctx = _ctx(["@someone check https://x.com 🔥", "omg 😭"])
    _, user = build_prompt(ctx, Profile(), "(none)")
    assert "@someone" not in user
    assert "https://x.com" not in user
    # the emoji/word signal survives
    assert "🔥" in user and "😭" in user


def test_caption_is_included_as_primary_signal():
    ctx = ReelContext(chat_name="x", reel_id="r1",
                      caption="3 apps to actually learn to code fast",
                      comments=[Comment("this goes crazy"), Comment("🔥🔥")])
    system, user = build_prompt(ctx, Profile(), "(none)")
    # the caption (content) reaches the model...
    assert "learn to code" in user
    # ...and the system prompt tells it to react to content, not the loudest comment
    assert "ACTUALLY ABOUT" in system
    # comments are still present but framed as secondary
    assert "secondary" in user.lower()


def test_caption_redacted_like_comments():
    ctx = ReelContext(chat_name="x", reel_id="r1",
                      caption="follow @guru at https://x.com for more",
                      comments=[Comment("nice")])
    _, user = build_prompt(ctx, Profile(), "(none)")
    assert "@guru" not in user and "https://x.com" not in user


def test_caption_only_reel_can_be_suggested():
    # No comments, but a caption is enough signal for the model to react.
    s = Settings()
    c = FakeClient(reply='{"reply":"😭","confidence":0.8,"should_reply":true}')
    ctx = ReelContext(chat_name="x", reel_id="r1",
                      caption="POV: mondays", comments=[])
    out = suggest_reply(ctx, Profile(), s, c)
    assert out is not None and out.reply_text == "😭"


def test_parse_accepts_clean_json():
    s = Settings()
    c = FakeClient(reply='{"reply": "😭😭", "confidence": 0.9, "should_reply": true}')
    out = suggest_reply(_ctx(["lol 😭", "dead 💀"]), Profile(), s, c)
    assert out is not None and out.reply_text == "😭😭"
    assert out.source == "llm"


def test_parse_handles_fenced_prose():
    s = Settings()
    c = FakeClient(reply='Sure!\n```json\n{"reply":"🔥","confidence":0.8,'
                         '"should_reply":true}\n```')
    out = suggest_reply(_ctx(["fire 🔥", "goes hard 🔥"]), Profile(), s, c)
    assert out is not None and out.reply_text == "🔥"


def test_should_reply_false_becomes_none():
    s = Settings()
    c = FakeClient(reply='{"reply":"", "confidence":0.1, "should_reply":false}')
    assert suggest_reply(_ctx(["huh"]), Profile(), s, c) is None


def test_rejects_reply_with_mention():
    s = Settings()
    c = FakeClient(reply='{"reply":"@bob 💀","confidence":0.95,"should_reply":true}')
    assert suggest_reply(_ctx(["lol"]), Profile(), s, c) is None


def test_rejects_overlong_reply():
    s = Settings(llm_max_reply_len=10)
    c = FakeClient(reply='{"reply":"this is way too long to be a reaction",'
                         '"confidence":0.95,"should_reply":true}')
    assert suggest_reply(_ctx(["lol"]), Profile(), s, c) is None


def test_low_confidence_keeps_flag():
    s = Settings(llm_min_confidence=0.6)
    c = FakeClient(reply='{"reply":"😭","confidence":0.4,"should_reply":true}')
    assert suggest_reply(_ctx(["lol"]), Profile(), s, c) is None


def test_none_client_returns_none():
    assert suggest_reply(_ctx(["lol"]), Profile(), Settings(), None) is None


def test_empty_comments_returns_none_without_calling():
    s = Settings()
    c = FakeClient(reply='{"reply":"😭","confidence":0.9,"should_reply":true}')
    assert suggest_reply(_ctx([]), Profile(), s, c) is None
    assert c.calls == []          # never wasted an API call on empty input


def test_client_exception_is_swallowed():
    c = FakeClient(raise_it=True)
    assert suggest_reply(_ctx(["lol 😭"]), Profile(), Settings(), c) is None


def test_profile_style_appears_in_prompt():
    p = Profile(emoji_prefs=["💀", "😭"], common_replies=["nah 😭"],
                reply_style=ReplyStyle.TEXT_EMOJI)
    _, user = build_prompt(_ctx(["lol"]), p, "(none)")
    assert "💀" in user and "nah 😭" in user and "text_emoji" in user
