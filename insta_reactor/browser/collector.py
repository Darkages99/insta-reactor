"""Read a reel's crowd comments from the web reel-viewer DOM.

Split in two so the tricky part is testable without a browser:
  * `parse_comment` / `parse_comments` — PURE functions over the raw innerText
    of each comment <li>. Fully unit-tested (see tests/test_browser_collector).
  * `collect_comments` — the Playwright side: scroll the comment list to load
    more, grab each <li>'s innerText, and hand it to the pure parser.

A web comment <li> renders roughly as (newlines collapsed here):
    <author>\n<comment text…>\n<age><N> likes Reply\n[See Translation]\n[View replies (N)]
The caption row and the "View replies" expanders have NO "Reply" button, which
is how we tell a real comment apart from them.
"""

from __future__ import annotations

import re
from typing import Optional

from ..models import Comment

_LIKES_RE = re.compile(r"([\d,]+)\s+likes?", re.IGNORECASE)
# The meta line of a comment mashes age + like-count + the Reply button together
# (e.g. "3 d23,442 likesReply"); it always contains "Reply" or "likes".
_META_TOKENS = ("Reply", "likes", "like")


def _parse_likes(text: str) -> int:
    m = _LIKES_RE.search(text or "")
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def parse_comment(li_text: str) -> Optional[Comment]:
    """Turn one comment <li>'s innerText into a Comment, or None if it isn't a
    real comment (caption, a 'View replies' expander, or an image-only reply
    with no text to react to)."""
    if not li_text:
        return None
    # A genuine comment row always has the "Reply" affordance; the caption and
    # the "View replies (N)"/"Hide replies" expanders do not.
    if "Reply" not in li_text:
        return None
    lines = [ln.strip() for ln in li_text.split("\n") if ln.strip()]
    if len(lines) < 2:
        return None
    author = lines[0]
    # The comment body is everything between the author and the meta line.
    meta_idx = None
    for i in range(1, len(lines)):
        if any(tok in lines[i] for tok in _META_TOKENS):
            meta_idx = i
            break
    if meta_idx is None or meta_idx < 1:
        return None
    text = " ".join(lines[1:meta_idx]).strip()
    if not text:
        return None                     # image/GIF-only reply — no text signal
    return Comment(text=text, likes=_parse_likes(li_text))


def parse_comments(li_texts: list[str]) -> list[Comment]:
    """Parse a list of <li> innerTexts into Comments, dropping non-comments."""
    out: list[Comment] = []
    for t in li_texts or []:
        c = parse_comment(t)
        if c is not None:
            out.append(c)
    return out


# The caption row (poster's own text) mashes trailing metadata onto its own
# lines: a relative age ("2 d", "5 hours ago"), like/view counts, and expanders
# like "more" / "See translation". None of that is the caption itself.
_CAPTION_META_RE = re.compile(
    r"^(?:\d[\d,]*\s*(?:likes?|views?|comments?)?"
    r"|\d+\s*[smhdw]"
    r"|\d+\s+(?:second|minute|hour|day|week|month|year)s?(?:\s+ago)?"
    r"|…?\s*more|edited|see translation|translated|reply)$",
    re.IGNORECASE,
)


def parse_caption(li_text: str, max_len: int = 500) -> str:
    """Extract the reel's caption body from the caption row's innerText.

    The caption row renders as `<poster handle>\\n<caption…>\\n<age>` with no
    "Reply" affordance (that's how the caption is told apart from a comment).
    We drop the leading handle and any trailing metadata lines, returning just
    the human caption text (trimmed to `max_len`). Pure + unit-tested.
    """
    if not li_text:
        return ""
    lines = [ln.strip() for ln in li_text.split("\n") if ln.strip()]
    if not lines:
        return ""
    # First line is the poster's handle; the caption is what follows.
    body = lines[1:] if len(lines) > 1 else []
    kept = [ln for ln in body if not _CAPTION_META_RE.match(ln)]
    return " ".join(kept).strip()[:max_len]


# --- Playwright extraction (not unit-tested; exercised live) ----------------

_EXTRACT_JS = """
() => {
  const dialog = document.querySelector('div[role="dialog"]') || document.body;
  const uls = [...dialog.querySelectorAll('ul')]
      .sort((a,b) => b.querySelectorAll('li').length - a.querySelectorAll('li').length);
  const seen = new Set();
  const out = [];
  for (const ul of uls) {
    for (const li of ul.querySelectorAll('li')) {
      const t = (li.innerText || '').trim();
      if (t && !seen.has(t)) { seen.add(t); out.push(t); }
    }
  }
  return out;
}
"""


def collect_comments(page, limit: int = 50, max_scrolls: int = 12):
    """Scroll the reel viewer's comment list and return (comments, count).

    `page` is a Playwright Page already on a /p/<code>/ or /reel/<code>/ view.
    Scrolls the comment panel to load up to ~`limit` comments, then parses them.
    Returns (list[Comment], reported_count) where reported_count is best-effort
    (len of what we read). Never raises — on any failure returns ([], None) so
    the reel is flagged unable_to_read rather than crashing the run.
    """
    try:
        prev = -1
        for _ in range(max_scrolls):
            texts = page.evaluate(_EXTRACT_JS)
            comments = parse_comments(texts)
            if len(comments) >= limit or len(comments) == prev:
                break
            prev = len(comments)
            # Scroll the comment region to load more (wheel over the dialog).
            try:
                page.mouse.wheel(0, 1500)
            except Exception:
                pass
            page.wait_for_timeout(400)
        texts = page.evaluate(_EXTRACT_JS)
        comments = parse_comments(texts)[:limit]
        return comments, (len(comments) or None)
    except Exception:
        return [], None


# The caption is the first list item in the reel dialog that is NOT a real
# comment: on the post/reel view IG renders it as the first <li> of the comment
# list, and (unlike a comment) it has no "Reply" affordance. We return its raw
# innerText for parse_caption to clean.
_CAPTION_JS = """
() => {
  const dialog = document.querySelector('div[role="dialog"]') || document.body;
  const uls = [...dialog.querySelectorAll('ul')]
      .sort((a,b) => b.querySelectorAll('li').length - a.querySelectorAll('li').length);
  for (const ul of uls) {
    for (const li of ul.querySelectorAll('li')) {
      const t = (li.innerText || '').trim();
      if (!t) continue;
      // A real comment always shows "Reply"; the caption never does. Skip the
      // "View replies (N)" / "Hide replies" expanders (short, contain 'replies').
      if (/\\bReply\\b/.test(t)) return "";       // hit comments before any caption
      if (/\\breplies\\b/i.test(t) && t.length < 40) continue;
      return t;                                    // first non-comment block = caption
    }
  }
  return "";
}
"""


def extract_caption(page, max_len: int = 500) -> str:
    """Read + clean the reel's caption from an open reel viewer. Never raises;
    returns "" when there's no caption (or on any failure)."""
    try:
        raw = page.evaluate(_CAPTION_JS)
    except Exception:
        return ""
    return parse_caption(raw or "", max_len=max_len)
