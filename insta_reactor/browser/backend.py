"""Browser backend — drive Instagram web via Playwright, implement `Backend`.

This is the DOM analogue of backends/android.py. It navigates and observes only;
the engine decides. Compared with the phone backend it is far simpler because the
web DOM is more structured than an accessibility node tree AND reels open at a
stable `/p/<shortcode>/` URL, giving a real per-reel id.

Safety: a `send_whitelist` (set of chat names) hard-caps where anything can be
sent. When set, `send_reply`/`send_reaction` refuse to act in any other chat and
return False (the runner then flags the reel). This is the guardrail that keeps
dev/testing confined to one authorised chat.
"""

from __future__ import annotations

import logging
import os
import re
import time

from ..backends.base import Backend, ReelHandle
from ..config import AppConfig
from ..models import ReelContext, Comment
from ..browser.driver import BrowserDriver
from ..browser import selectors as S
from ..browser.collector import collect_comments, extract_caption

log = logging.getLogger("insta_reactor.browser.backend")

# JS that returns one descriptor per shared reel currently in the thread DOM,
# oldest-first (DOM order). `sender` is who sent the message (the suffix of the
# "React to message from <sender>" hover control), so the caller can tell an
# incoming reel from our own outgoing one. `box` is the reel bubble's viewport
# rect, used to click it open and to screenshot its thumbnail.
_REELS_JS = """
() => {
  // The DM thread pane has no stable role wrapper on all layouts, so query the
  // whole document; the only "Clip"-labelled nodes on a DM page are reels.
  const clips = [...document.querySelectorAll('[aria-label="Clip"]')];
  const out = [];
  clips.forEach((clip, i) => {
    // The [aria-label="Clip"] node is just the small play-icon SVG. Climb from
    // it to (a) the sender (hover "React to message from…") and (b) the reel's
    // image container — the innermost portrait-ish ancestor — which is the right
    // click/screenshot target. Clicking the tiny icon or the huge message row
    // both miss; the reel bubble is what opens the viewer.
    let node = clip, sender = null, box = null;
    for (let up = 0; up < 12 && node; up++) {
      if (!sender) {
        const rb = node.querySelector('[aria-label^="React to message from "]');
        if (rb) sender = rb.getAttribute('aria-label')
                            .replace('React to message from ', '');
      }
      if (!box) {
        const r = node.getBoundingClientRect();
        if (r.width >= 120 && r.width <= 600 &&
            r.height >= 150 && r.height <= 800 && r.height >= r.width) {
          box = {x: r.x, y: r.y, w: r.width, h: r.height};
        }
      }
      node = node.parentElement;
    }
    if (box === null) {
      const r = clip.getBoundingClientRect();
      box = {x: r.x, y: r.y, w: r.width, h: r.height};
    }
    out.push({index: i, sender: sender, box: box});
  });
  return out;
}
"""


def _norm_emoji(e: str) -> str:
    return (e or "").replace("️", "").strip()


class BrowserBackend(Backend):
    def __init__(self, config: AppConfig, driver: BrowserDriver | None = None,
                 send_whitelist: set[str] | None = None,
                 target_direction: str = "incoming",
                 self_username: str = "",
                 thumbs_dir: str = os.path.join("data", "thumbs")):
        self.config = config
        self.driver = driver or BrowserDriver(
            headless=getattr(config.settings, "browser_headless", False))
        # None => sends allowed anywhere (production). A set => sends only in
        # those chats; everywhere else send_* refuses. THE testing guardrail.
        self.send_whitelist = send_whitelist
        # "incoming" => only react to reels others sent us (production default).
        # "any" => react regardless of direction (used for the single-account
        # test chat, whose reels are outgoing but still fully reactable).
        self.target_direction = target_direction
        self.self_username = self_username
        self.thumbs_dir = thumbs_dir
        self._chat = ""

    # ---- lifecycle ------------------------------------------------------
    def prepare(self) -> None:
        self.driver.start()
        if not self.driver.ensure_logged_in():
            raise RuntimeError(
                "Instagram is not logged in in the automation browser. Log in "
                "once in the opened window, then re-run.")
        if not self.self_username:
            self.self_username = self.driver.self_username()
        log.info("browser ready; self_username=%r", self.self_username)

    @property
    def page(self):
        return self.driver.page

    # ---- navigation -----------------------------------------------------
    # JS: dismiss screen-time / well-being interstitials ("You're in sleep
    # mode", "You've reached your daily limit", "Take a break"). These cover the
    # whole thread and INTERCEPT every reel-open click — the top cause of
    # reels wrongly flagged "unable to read" — and their only control is an "OK"
    # button, which the "Not Now"/Close handling below never matches. Clicks the
    # dialog's dismiss button in-page; returns how many it closed.
    _DISMISS_SCREENTIME_JS = """
    () => {
      let n = 0;
      const RX = /(sleep mode|daily limit|reached your daily|you've been on instagram|take a break|taking a break|time to close)/i;
      const clickable = (el) => {
        for (let i = 0; i < 4 && el; i++) {
          if (el && (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button')) return el;
          el = el && el.parentElement;
        }
        return null;
      };
      // Case 1: role=dialog nags ("You're in sleep mode") with an OK/Continue button.
      for (const d of document.querySelectorAll('div[role="dialog"]')) {
        if (!RX.test(d.innerText || '')) continue;
        const btns = [...d.querySelectorAll('button, [role="button"], a[role="button"]')];
        const hit = btns.find(b => /^(ok|got it|continue|dismiss|close|not now)$/i
                                     .test((b.innerText || '').trim()));
        const target = hit || btns[btns.length - 1];
        if (target) { target.click(); n++; }
      }
      if (n) return n;
      // Case 2: full-page interstitial ("You've reached your daily limit") that is
      // NOT a role=dialog. Only treat it as blocking if a screen-time overlay
      // actually COVERS the viewport centre (avoids false-firing on residual
      // hidden text elsewhere in the DOM). Then click its Close (X).
      const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
      let root = null;
      for (let a = el; a; a = a.parentElement) {
        const r = a.getBoundingClientRect();
        if (r.width > innerWidth * 0.8 && r.height > innerHeight * 0.8
            && RX.test(a.innerText || '')) { root = a; break; }
      }
      if (root) {
        const x = root.querySelector('[aria-label="Close"], svg[aria-label="Close"]')
               || document.querySelector('[aria-label="Close"], svg[aria-label="Close"]');
        const btn = x && (clickable(x) || x);
        if (btn) { btn.click(); n++; }
      }
      return n;
    }
    """

    def _kill_screentime(self) -> int:
        """Dismiss any screen-time well-being modal currently up. Cheap + safe to
        call liberally (it only touches dialogs whose text matches the nag
        phrases). Returns how many it closed; never raises."""
        try:
            return int(self.page.evaluate(self._DISMISS_SCREENTIME_JS) or 0)
        except Exception:
            return 0

    def _dismiss_overlays(self) -> None:
        """Clear interstitials that overlay the inbox/thread and intercept
        clicks: post-login ("Save your login info?", "Turn on notifications")
        AND screen-time well-being nags ("You're in sleep mode", "daily limit").
        Best-effort; never raises."""
        # Screen-time modals first — they're full-screen and block everything.
        if self._kill_screentime():
            self.page.wait_for_timeout(300)
        for txt in ("Not Now", "Not now", "Not now."):
            try:
                btn = self.page.locator(
                    f'button:has-text("{txt}"), div[role="button"]:has-text("{txt}")')
                if btn.count():
                    btn.first.click(timeout=2000)
                    self.page.wait_for_timeout(500)
            except Exception:
                pass
        # Other modals expose only a Close (X) control — click it, then Escape
        # as a catch-all.
        try:
            close = self.page.locator(
                '[aria-label="Close"], svg[aria-label="Close"]')
            if close.count():
                close.last.click(timeout=2000)
                self.page.wait_for_timeout(400)
        except Exception:
            pass
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)
        except Exception:
            pass

    # JS: click the inbox thread ROW whose name matches. The name appears in
    # several places (notes carousel, etc.), so we climb from each matching
    # <span> to the enclosing row (a wide, row-height clickable) and click THAT,
    # which fires IG's React navigation. Returns whether it clicked something.
    _OPEN_THREAD_JS = """
    (name) => {
      const spans = [...document.querySelectorAll('span')]
          .filter(s => (s.textContent || '').trim() === name);
      for (const s of spans) {
        let n = s;
        for (let i = 0; i < 9 && n; i++) {
          const r = n.getBoundingClientRect();
          // A thread row is wide and roughly one row tall.
          if (r.width > 240 && r.height >= 48 && r.height <= 130) {
            n.click();
            return true;
          }
          n = n.parentElement;
        }
      }
      return false;
    }
    """

    def _on_thread(self) -> bool:
        return "/direct/t/" in (self.page.url or "")

    def open_chat(self, chat_name: str) -> bool:
        page = self.page
        try:
            page.goto("https://www.instagram.com/direct/inbox/",
                      wait_until="domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(1500)
        self._dismiss_overlays()
        # Primary: click the thread row straight from the inbox list. Robust to
        # the search box not filtering under automation and to no-avatar chats.
        if self._open_from_inbox_list(chat_name):
            return True
        # Fallback: the search box (thread may be scrolled out of the inbox).
        try:
            self._kill_screentime()   # else the modal swallows the box click
            box = page.locator('input[placeholder="Search"]')
            box.click()
            box.fill(chat_name)
            page.wait_for_timeout(1800)
            if page.evaluate(self._OPEN_THREAD_JS, chat_name):
                page.wait_for_timeout(2000)
                if self._on_thread():
                    self._chat = chat_name
                    self._scroll_thread_to_bottom()
                    return True
            log.warning("chat %r not found (inbox list + search)", chat_name)
            return False
        except Exception:
            log.exception("open_chat failed for %r", chat_name)
            return False

    def _open_from_inbox_list(self, chat_name: str) -> bool:
        """Open a thread by clicking its inbox row via JS, verified by the URL
        becoming /direct/t/<id>. Never raises."""
        page = self.page
        try:
            if not self._on_thread():
                pass
            clicked = page.evaluate(self._OPEN_THREAD_JS, chat_name)
            if not clicked:
                return False
            page.wait_for_timeout(2000)
            if not self._on_thread():
                return False
            self._chat = chat_name
            self._scroll_thread_to_bottom()
            return True
        except Exception:
            log.exception("_open_from_inbox_list failed for %r", chat_name)
            return False

    def return_to_inbox(self) -> None:
        try:
            self.driver.goto_inbox()
        except Exception:
            log.exception("return_to_inbox failed")

    def _scroll_thread_to_bottom(self) -> None:
        try:
            self.page.keyboard.press("End")
            self.page.wait_for_timeout(400)
        except Exception:
            pass

    # ---- reel enumeration ----------------------------------------------
    def _reel_descriptors(self) -> list[dict]:
        try:
            return self.page.evaluate(_REELS_JS) or []
        except Exception:
            log.exception("reel enumeration failed")
            return []

    def _is_target(self, desc: dict) -> bool:
        if self.target_direction == "any":
            return True
        sender = (desc.get("sender") or "").strip()
        # incoming = sent by someone other than us. If we don't know our own
        # handle, we can't be sure, so treat unknown-sender as incoming.
        if not sender:
            return True
        return self.self_username == "" or sender != self.self_username

    def find_unreacted_reels(self) -> list[ReelHandle]:
        """Interface method; the runner uses iter_reels(). Returns target reels
        currently in the thread, newest-first."""
        descs = [d for d in self._reel_descriptors() if self._is_target(d)]
        return [ReelHandle(reel_id=f"reel#{d['index']}", locator=d)
                for d in reversed(descs)]

    def iter_reels(self):
        """Yield (ReelHandle, ReelContext) for the newest target reels.

        Newest-first, capped at settings.max_new_reels. For each reel we capture
        a thumbnail, open it to read its comments + capture the stable shortcode,
        then return to the thread and yield. The reel is identified by its index
        among the thread's reel bubbles, re-resolved fresh for any later send()."""
        cap = getattr(self.config.settings, "max_new_reels", 3)
        # Reel bubbles can render their [aria-label="Clip"] a beat after the
        # thread loads — retry a few times before concluding there are none.
        descs = []
        for _ in range(4):
            # A screen-time nag can cover the thread and stop reels from
            # rendering/being found — clear it each attempt before enumerating.
            self._kill_screentime()
            descs = [d for d in self._reel_descriptors() if self._is_target(d)]
            if descs:
                break
            self.page.wait_for_timeout(1200)
        targets = list(reversed(descs))[:cap]     # newest-first
        log.info("iter_reels: %d reel(s) in thread, %d target(s)",
                 len(descs), len(targets))
        for d in targets:
            idx = d["index"]
            # Primary thumbnail: a clip of the DM reel bubble taken from the
            # thread (crisp at 2x, and shows the real cover frame). The viewer
            # cover is only a fallback — the reel's <video> is often an unrendered
            # black frame when we screenshot it.
            bubble = self._capture_thumbnail(d, idx)
            ctx = self._open_and_read(d, idx)   # reads comments+caption, cover fallback
            if bubble:
                ctx.thumbnail_path = bubble
            yield ReelHandle(reel_id=ctx.reel_id or f"reel#{idx}",
                             locator={"index": idx}), ctx

    def _capture_thumbnail(self, desc: dict, idx: int) -> str | None:
        box = desc.get("box") or {}
        if not box or box.get("w", 0) < 8 or box.get("h", 0) < 8:
            return None
        os.makedirs(self.thumbs_dir, exist_ok=True)
        safe_chat = re.sub(r"[^\w.-]", "_", self._chat)[:40]
        path = os.path.join(self.thumbs_dir,
                            f"{safe_chat}_{idx}_{int(time.time())}.png")
        try:
            self.page.screenshot(path=path, clip={
                "x": max(0, box["x"]), "y": max(0, box["y"]),
                "width": box["w"], "height": box["h"]})
            return path
        except Exception:
            log.exception("thumbnail capture failed")
            return None

    # JS: rect of the largest media element (img/video) inside the reel viewer
    # dialog — i.e. the reel's cover. Much clearer than a clip of the DM bubble.
    _COVER_JS = """
    () => {
      const dialog = document.querySelector('div[role="dialog"]') || document.body;
      const pick = (sel) => {
        let best = null, area = 0;
        for (const n of dialog.querySelectorAll(sel)) {
          const r = n.getBoundingClientRect();
          const a = r.width * r.height;
          if (a > area) { area = a; best = r; }
        }
        return best;
      };
      // Prefer the poster <img> (an actual cover frame) over the <video>, which
      // is frequently an unrendered black frame at screenshot time.
      const best = pick('img') || pick('video');
      return best ? {x: best.x, y: best.y, w: best.width, h: best.height} : null;
    }
    """

    def _capture_caption(self) -> str:
        """Best-effort read of the open reel's caption (its content signal).
        Empty string on any failure — the pipeline then works comments-only."""
        try:
            return extract_caption(self.page)
        except Exception:
            log.exception("caption capture failed")
            return ""

    def _capture_cover(self, tag) -> str | None:
        """Screenshot the reel's cover from the OPEN viewer. Returns a path or
        None (caller then falls back to the DM-bubble clip). Never raises."""
        try:
            box = self.page.evaluate(self._COVER_JS)
        except Exception:
            box = None
        if not box or box.get("w", 0) < 40 or box.get("h", 0) < 40:
            return None
        os.makedirs(self.thumbs_dir, exist_ok=True)
        safe_chat = re.sub(r"[^\w.-]", "_", self._chat)[:40]
        safe_tag = re.sub(r"[^\w.-]", "_", str(tag))[:24]
        path = os.path.join(self.thumbs_dir,
                            f"{safe_chat}_{safe_tag}_{int(time.time())}.png")
        try:
            self.page.screenshot(path=path, clip={
                "x": max(0, box["x"]), "y": max(0, box["y"]),
                "width": box["w"], "height": box["h"]})
            return path
        except Exception:
            log.exception("cover capture failed")
            return None

    # JS: scroll the idx-th reel bubble into the viewport centre and return its
    # FRESH rect. Enumeration snapshots boxes once, but iter_reels scrolls the
    # thread to the bottom first, so an older reel's cached box can be above the
    # viewport — clicking those stale coords misses and the reel mis-flags
    # "unable to read". Re-resolving after scrollIntoView fixes that.
    _SCROLL_REEL_JS = """
    (idx) => {
      const clips = [...document.querySelectorAll('[aria-label="Clip"]')];
      const clip = clips[idx];
      if (!clip) return null;
      let node = clip, box = null, bubble = null;
      for (let up = 0; up < 12 && node; up++) {
        const r = node.getBoundingClientRect();
        if (r.width >= 120 && r.width <= 600 &&
            r.height >= 150 && r.height <= 800 && r.height >= r.width) {
          bubble = node; break;
        }
        node = node.parentElement;
      }
      const target = bubble || clip;
      target.scrollIntoView({block: 'center', inline: 'center'});
      const r = target.getBoundingClientRect();
      return {x: r.x, y: r.y, w: r.width, h: r.height};
    }
    """

    def _open_and_read(self, desc: dict, idx: int) -> ReelContext:
        box = desc.get("box") or {}
        # Re-resolve the reel's box after scrolling it into view — its cached
        # coords may be off-screen (thread was scrolled to the bottom), which
        # makes the open-click miss and the reel wrongly flag "unable to read".
        try:
            fresh = self.page.evaluate(self._SCROLL_REEL_JS, idx)
            if fresh and fresh.get("w", 0) > 8 and fresh.get("h", 0) > 8:
                box = fresh
                self.page.wait_for_timeout(400)
        except Exception:
            log.exception("scroll-into-view failed for reel #%d", idx)
        cx = box.get("x", 0) + box.get("w", 0) / 2
        cy = box.get("y", 0) + box.get("h", 0) / 2
        # A center click on a tall reel bubble lands where IG's hover controls
        # (React/Reply/More) appear and just *reveals* them instead of opening the
        # reel; clicking the UPPER portion of the cover opens it reliably (verified
        # live). Try upper-third first, then center as a fallback.
        upper_y = box.get("y", 0) + box.get("h", 0) * 0.3
        try:
            # A screen-time nag ("sleep mode"/"daily limit") can pop up mid-run
            # and sits on top of the thread, swallowing this click. Clear it
            # first — otherwise the reel never opens and we mis-flag it
            # "unable to read".
            self._kill_screentime()
            self.page.mouse.click(cx, upper_y)
            self.page.wait_for_timeout(1200)
            shortcode = self._shortcode_from_url()
            if not shortcode:
                # upper click may have hovered/missed; retry at the bubble centre
                self.page.mouse.click(cx, cy)
                self.page.wait_for_timeout(1200)
                shortcode = self._shortcode_from_url()
            if not shortcode:
                log.warning("reel #%d did not open to a /p|reel/ url", idx)
                return ReelContext(chat_name=self._chat, reel_id=f"reel#{idx}",
                                   read_error=True)
            comments, count = collect_comments(
                self.page, limit=self.config.settings.comments_to_read)
            caption = self._capture_caption()
            cover = self._capture_cover(shortcode or idx)
            self._close_reel_viewer()
            return ReelContext(
                chat_name=self._chat, reel_id=shortcode,
                comments=comments, comment_count=count,
                caption=caption, thumbnail_path=cover,
                read_error=(comments == [] and count is None))
        except Exception:
            log.exception("_open_and_read failed for reel #%d", idx)
            self._close_reel_viewer()
            return ReelContext(chat_name=self._chat, reel_id=f"reel#{idx}",
                               read_error=True)

    def _shortcode_from_url(self) -> str:
        m = re.search(S.REEL_URL_RE, self.page.url or "")
        return m.group(1) if m else ""

    def _close_reel_viewer(self) -> None:
        try:
            if re.search(S.REEL_URL_RE, self.page.url or ""):
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(800)
            if re.search(S.REEL_URL_RE, self.page.url or ""):
                # still on the reel — navigate back to the thread explicitly
                self.page.go_back()
                self.page.wait_for_timeout(800)
        except Exception:
            log.exception("closing reel viewer failed")

    def build_reel_context(self, reel: ReelHandle) -> ReelContext:
        idx = (reel.locator or {}).get("index", 0) if isinstance(reel.locator, dict) else 0
        descs = self._reel_descriptors()
        desc = next((d for d in descs if d["index"] == idx), None)
        if desc is None:
            return ReelContext(chat_name=self._chat, reel_id=reel.reel_id,
                               read_error=True)
        return self._open_and_read(desc, idx)

    def discard_reel(self, reel: ReelHandle) -> None:
        self._close_reel_viewer()

    # ---- sending (guarded) ---------------------------------------------
    def _send_allowed(self) -> bool:
        if self.send_whitelist is None:
            return True
        if self._chat in self.send_whitelist:
            return True
        log.error("SEND BLOCKED: chat %r not in send whitelist %r — refusing to "
                  "send.", self._chat, sorted(self.send_whitelist))
        return False

    def _reel_locator_by_index(self, idx: int):
        descs = self._reel_descriptors()
        return next((d for d in descs if d["index"] == idx), None)

    def _hover_reveal_controls(self, desc: dict) -> None:
        box = desc.get("box") or {}
        cx = box.get("x", 0) + box.get("w", 0) / 2
        cy = box.get("y", 0) + box.get("h", 0) / 2
        self.page.mouse.move(cx, cy)
        self.page.wait_for_timeout(500)

    def send_reaction(self, reel: ReelHandle, emoji: str) -> bool:
        if not self._send_allowed():
            return False
        if _norm_emoji(emoji) not in {_norm_emoji(e) for e in S.QUICK_REACTION_EMOJIS}:
            return False   # not a one-tap emoji; let the runner fall back to reply
        idx = (reel.locator or {}).get("index", 0)
        desc = self._reel_locator_by_index(idx)
        if desc is None:
            return False
        try:
            self._hover_reveal_controls(desc)
            react = self.page.locator(
                f'[aria-label^="{S.REACT_BUTTON_PREFIX}"]').last
            react.click()
            self.page.wait_for_timeout(500)
            target = _norm_emoji(emoji)
            btn = self.page.locator('[role="button"]').filter(
                has_text=re.compile(re.escape(target))).last
            btn.click()
            self.page.wait_for_timeout(600)
            log.info("reacted %s to reel #%d in %r", emoji, idx, self._chat)
            return True
        except Exception:
            log.exception("send_reaction failed")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def send_reply(self, reel: ReelHandle, text: str) -> bool:
        if not self._send_allowed():
            return False
        idx = (reel.locator or {}).get("index", 0)
        desc = self._reel_locator_by_index(idx)
        try:
            # Quote the specific reel so the reply attaches to it (not a floating
            # thread message). Falls back to the plain composer if the Reply
            # control can't be armed.
            if desc is not None:
                try:
                    self._hover_reveal_controls(desc)
                    self.page.locator(
                        f'[aria-label^="{S.REPLY_BUTTON_PREFIX}"]').last.click()
                    self.page.wait_for_timeout(400)
                except Exception:
                    log.info("reply-quote control not armed; using plain composer")
            composer = self.page.locator(S.COMPOSER).last
            composer.click()
            composer.type(text, delay=25)
            self.page.wait_for_timeout(300)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(800)
            log.info("replied %r to reel #%d in %r", text, idx, self._chat)
            return True
        except Exception:
            log.exception("send_reply failed")
            return False

    def close(self) -> None:
        try:
            self.driver.close()
        except Exception:
            pass
