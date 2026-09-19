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
from ..browser.watermark import ReelWatermarkStore
from ..paths import data_path

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
  const MID_RE = /\\/(\\d{6,})_(\\d{6,})_/;
  // Stable per-reel id from a cover <img> src: the fbcdn media id if present,
  // else the query-stripped path hashed to a short token. EITHER way distinct
  // reels get distinct ids that survive scroll/mount churn (used for dedup and
  // the reacted-watermark, see watermark.py). Kept identical across the three
  // reel-finding JS blocks so an id computed in one matches the others.
  const idFromSrc = (src) => {
    src = src || '';
    const m = src.match(MID_RE);
    if (m) return m[1] + '_' + m[2];
    let path = src;
    try { path = new URL(src, location.href).pathname; } catch (e) { path = src.split('?')[0]; }
    let h = 0;
    for (let i = 0; i < path.length; i++) { h = (h * 31 + path.charCodeAt(i)) | 0; }
    return 'p:' + (h >>> 0).toString(36);
  };
  const clips = [...document.querySelectorAll('[aria-label="Clip"]')];
  const out = [];
  clips.forEach((clip, i) => {
    // The [aria-label="Clip"] node is just the small play-icon SVG. Climb from
    // it to (a) the sender (hover "React to message from…"), (b) the reel's
    // COVER IMAGE — the reliable click/screenshot target — and (c) a
    // portrait-ish ancestor as a secondary box. Clicking the tiny icon or the
    // huge message row both miss; the cover is what opens the viewer.
    let node = clip, sender = null, portrait = null, justifyEnd = false;
    let coverImg = null, coverRect = null;
    for (let up = 0; up < 12 && node; up++) {
      if (!sender) {
        const rb = node.querySelector('[aria-label^="React to message from "]');
        if (rb) sender = rb.getAttribute('aria-label')
                            .replace('React to message from ', '');
      }
      // Cover image: prefer an img whose src carries a media id; else the
      // largest img in scope. Its rect is the durable target.
      if (!coverImg && node.querySelector) {
        const imgs = [...node.querySelectorAll('img[src]')];
        const mided = imgs.find(im => MID_RE.test(im.getAttribute('src') || ''));
        const pick = mided || imgs.sort((a, b) => {
          const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
          return rb.width * rb.height - ra.width * ra.height;
        })[0];
        if (pick) { coverImg = pick; coverRect = pick.getBoundingClientRect(); }
      }
      if (!portrait) {
        const r = node.getBoundingClientRect();
        if (r.width >= 120 && r.width <= 600 &&
            r.height >= 150 && r.height <= 900 && r.height >= r.width) {
          portrait = {x: r.x, y: r.y, w: r.width, h: r.height};
        }
      }
      // Own (outgoing) messages are right-aligned in the thread — their row
      // uses justify-content:flex-end. This is the durable "mine vs theirs"
      // signal (the "React to message from <name>" label carries a DISPLAY name
      // that need not equal the logged-in @handle, so name-matching alone
      // wrongly treats your own reels as incoming). OR'd with a geometric
      // right-of-centre check below.
      if (!justifyEnd) {
        try {
          const jc = getComputedStyle(node).justifyContent || '';
          if (jc === 'flex-end' || jc === 'end' || jc === 'right') justifyEnd = true;
        } catch (e) {}
      }
      node = node.parentElement;
    }
    // Choose the click/screenshot box: the cover image rect when it's a real,
    // sizeable portrait cover; else the portrait ancestor.
    let box = null;
    if (coverRect && coverRect.width >= 100 && coverRect.height >= 150) {
      box = {x: coverRect.x, y: coverRect.y, w: coverRect.width, h: coverRect.height};
    } else if (portrait) {
      box = portrait;
    }
    // GATE: only a real, sizeable shared-reel cover counts. A missing/tiny cover
    // means this "Clip" node is a reply-quote PREVIEW (the little thumbnail
    // inside our own reply message) or other chrome — NOT a freshly shared reel.
    // Dropping those is what stops the bot replying to its own reply-quotes and
    // mis-flagging tiny targets "unable to read" (their click landed on the
    // play-icon and missed).
    if (!box || box.h < 150 || box.w < 90) return;
    // Geometry fallback for "mine": the reel bubble sits on the right half of
    // the message pane, with a clear margin so a centred/unknown bubble isn't
    // misjudged. We anchor the pane to the composer (a textbox that always
    // exists on an open thread) because div[role="main"] is absent on some IG
    // layouts (verified live — it returned null, leaving this net dead).
    let rightAligned = false;
    const pane = document.querySelector(
      'div[role="textbox"][contenteditable="true"]')
      || document.querySelector('div[role="main"]');
    if (pane) {
      const m = pane.getBoundingClientRect();
      const paneMid = m.x + m.width / 2;
      const bubbleMid = box.x + box.w / 2;
      rightAligned = bubbleMid > paneMid + m.width * 0.08;
    }
    const mine = justifyEnd || rightAligned;
    const mid = coverImg ? idFromSrc(coverImg.getAttribute('src')) : null;
    // `total` is the count of reel bubbles CURRENTLY MOUNTED (the list is
    // virtualized, so this is NOT the thread's true reel count — don't rely on
    // it for anything but a rough newest-first ordering hint).
    out.push({index: i, total: clips.length, sender: sender, box: box,
              mid: mid, y: box.y, mine: mine});
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
                 thumbs_dir: str = data_path("thumbs"),
                 preload_min_reels: int = 0,
                 profile_dir: str | None = None,
                 watermark_store: ReelWatermarkStore | None = None,
                 live_frame_path: str | None = None,
                 should_stop=None):
        self.config = config
        # Manual kill switch. `should_stop()` True (the web UI's Stop button)
        # aborts the whole run; `stop_reason` is read back by the runner so the
        # UI can say why. This is distinct from the boundary stop below (which is
        # the normal "no more NEW reels here" end of a chat's sweep).
        self.should_stop = should_stop or (lambda: False)
        self.stop_reason: str | None = None
        # Our own actions this chat, so the boundary detector never mistakes the
        # bot's own reply/reaction for YOUR "already seen up to here" marker.
        self._bot_sent_texts: set = set()
        self._bot_reacted_mids: set = set()
        # When set (the web panel sets it), the backend drops a fresh full-page
        # PNG here at each natural pause so the UI can show a live "watch the bot
        # work" view. None => no snapshots (CLI / tests). Best-effort, never
        # affects the run.
        self.live_frame_path = live_frame_path
        driver_kwargs = {"headless": getattr(config.settings, "browser_headless", False)}
        # profile_dir picks WHICH Instagram account's persistent login this run
        # drives (each account gets its own Chromium user-data dir, so logins
        # never clobber each other). None => BrowserDriver's own default account.
        if profile_dir:
            driver_kwargs["profile_dir"] = profile_dir
        self.driver = driver or BrowserDriver(**driver_kwargs)
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
        # >0 only for bulk-eval runs (browser-run --preload N): scroll the thread
        # upward after opening it to lazy-load older reels before enumerating, so
        # a wide quality sweep isn't limited to whatever's already rendered.
        # Normal/production runs leave this 0 — no extra scrolling, no extra risk.
        self.preload_min_reels = preload_min_reels
        # Cross-run "already reacted up to here" per chat — see watermark.py.
        # Lets max_new_reels be raised to clear a whole burst of reels without
        # risking re-reacting to ones a prior run already handled.
        self.watermark = watermark_store or ReelWatermarkStore()

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
        self._snap_live()

    def _snap_live(self) -> None:
        """Drop a fresh full-page PNG at live_frame_path for the UI's live view.
        Writes to a temp file then atomically replaces, so a poller never serves
        a half-written frame. Runs on the Playwright thread (the only one that may
        touch `page`). Best-effort; never raises, never affects the run."""
        path = self.live_frame_path
        if not path:
            return
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            self.page.screenshot(path=tmp, type="png")
            os.replace(tmp, path)
        except Exception:
            log.warning("live-frame snapshot failed", exc_info=True)

    def _wait(self, ms: int) -> None:
        """`page.wait_for_timeout` plus a live-view snapshot right after — every
        pause the bot already takes doubles as a frame grab, so the web UI's
        monitor tracks what the browser is actually doing instead of only
        updating at a handful of lifecycle points."""
        self.page.wait_for_timeout(ms)
        self._snap_live()

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
            self._wait(300)
        for txt in ("Not Now", "Not now", "Not now."):
            try:
                btn = self.page.locator(
                    f'button:has-text("{txt}"), div[role="button"]:has-text("{txt}")')
                if btn.count():
                    btn.first.click(timeout=2000)
                    self._wait(500)
            except Exception:
                pass
        # Other modals expose only a Close (X) control — click it, then Escape
        # as a catch-all.
        try:
            close = self.page.locator(
                '[aria-label="Close"], svg[aria-label="Close"]')
            if close.count():
                close.last.click(timeout=2000)
                self._wait(400)
        except Exception:
            pass
        try:
            self.page.keyboard.press("Escape")
            self._wait(200)
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
        # A cold session (first navigation right after prepare()) sometimes
        # renders the inbox row list a beat late — the list/search checks below
        # both see an empty DOM and correctly report "not found", but it's really
        # just not painted yet. Retry the whole lookup a couple of times with a
        # longer settle wait before concluding the chat truly doesn't exist.
        for attempt in range(3):
            if self._open_chat_once(chat_name, extra_wait=attempt * 1500):
                return True
        log.warning("chat %r not found after retries (inbox list + search)",
                    chat_name)
        return False

    def _open_chat_once(self, chat_name: str, extra_wait: int = 0) -> bool:
        page = self.page
        try:
            page.goto("https://www.instagram.com/direct/inbox/",
                      wait_until="domcontentloaded")
        except Exception:
            pass
        self._wait(1500 + extra_wait)
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
            self._wait(1800)
            if page.evaluate(self._OPEN_THREAD_JS, chat_name):
                self._wait(2000)
                if self._on_thread():
                    self._chat = chat_name
                    self._scroll_thread_to_bottom()
                    return True
            return False
        except Exception:
            log.exception("_open_chat_once failed for %r", chat_name)
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
            self._wait(2000)
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

    def _viewport(self) -> tuple[int, int]:
        vp = getattr(self.page, "viewport_size", None) or {}
        return int(vp.get("width", 1400)), int(vp.get("height", 900))

    def _wheel(self, dy: int) -> None:
        """Scroll the DM message pane by `dy` px with a REAL mouse wheel over its
        centre. The thread is a virtualized, column-reverse container whose
        `scrollTop` is not programmatically settable (assignments are silently
        ignored — verified live), so the wheel is the only thing that actually
        moves it. `dy < 0` scrolls UP toward older messages. Never raises."""
        try:
            w, h = self._viewport()
            self.page.mouse.move(w * 0.5, h * 0.45)
            self.page.mouse.wheel(0, dy)
        except Exception:
            pass
        self._snap_live()

    def _scroll_thread_to_bottom(self) -> None:
        try:
            self.page.keyboard.press("End")
            self._wait(400)
            # End alone sometimes leaves us a few messages short of the very
            # newest; a wheel-down nudge pins us to the bottom (newest) reels.
            self._wheel(2400)
            self._wait(400)
        except Exception:
            pass
        if self.preload_min_reels > 0:
            self._load_history(self.preload_min_reels)

    def _mounted_mids(self) -> set:
        """Media ids of the target reels currently mounted in the DOM."""
        return {d.get("mid") for d in self._reel_descriptors()
                if self._is_target(d) and d.get("mid")}

    def _load_history(self, min_reels: int, max_attempts: int = 40) -> None:
        """Wheel the open thread upward until at least `min_reels` DISTINCT reels
        (by media id) have been seen, or scrolling stops revealing new ones.

        Counting *mounted* bubbles doesn't work — the list is virtualized, so
        the mounted count stays ~constant while different reels rotate through.
        We instead accumulate distinct media ids across scroll steps. Only used
        for bulk-eval sweeps (browser-run --preload N); never raises."""
        seen: set = set()
        stall = 0
        for _ in range(max_attempts):
            self._kill_screentime()
            before = len(seen)
            seen |= self._mounted_mids()
            if len(seen) >= min_reels:
                return
            self._wheel(-1400)
            self._wait(800)
            seen |= self._mounted_mids()
            stall = 0 if len(seen) > before else stall + 1
            if stall >= 3:
                return   # reached the top / no more history

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
        # PRIMARY signal: message alignment. Our own outgoing reels are
        # right-aligned (justify-content:flex-end / right of the pane centre);
        # incoming reels are left-aligned. This is reliable regardless of
        # whether we know our own @handle, and is the fix for "reacts to itself"
        # (the old name-compare failed whenever self_username was blank or the
        # hover label used a display name != the handle).
        if desc.get("mine"):
            return False
        # SECONDARY signal (belt-and-braces): if we DO know our handle and the
        # hover label names us as the sender, it's ours too.
        sender = (desc.get("sender") or "").strip()
        if sender and self.self_username and sender == self.self_username:
            return False
        return True

    def _mounted_targets(self) -> list[dict]:
        """Target reels currently mounted in the DOM, NEWEST-FIRST.

        The newest reel sits nearest the composer at the bottom, i.e. the
        largest viewport `y`. DOM order is unreliable for newest/oldest under
        virtualization, so we order by `y` descending."""
        descs = [d for d in self._reel_descriptors() if self._is_target(d)]
        descs.sort(key=lambda d: d.get("y", 0), reverse=True)
        return descs

    def find_unreacted_reels(self) -> list[ReelHandle]:
        """Interface method; the runner uses iter_reels(). Returns target reels
        currently mounted in the thread, newest-first."""
        return [ReelHandle(reel_id=d.get("mid") or f"reel#{d['index']}",
                           locator={"mid": d.get("mid"), "box": d.get("box")})
                for d in self._mounted_targets()]

    def _settle_reels(self) -> None:
        """Wait for the bottom reel bubbles to finish mounting. A burst of shares
        paints over a beat or two; poll until the mounted set stops growing."""
        last, stall = -1, 0
        for _ in range(8):
            self._kill_screentime()
            n = len(self._mounted_targets())
            if n == last:
                stall += 1
                if n and stall >= 2:
                    return
            else:
                stall = 0
            last = n
            self._wait(1000)

    def _pick_next(self, processed: set) -> tuple[dict | None, int]:
        """The newest currently-mounted target reel we haven't handled — not
        already yielded this run (`processed`) and not reacted on a prior run
        (watermark) — plus its newest-first rank (0 = newest). (None, 0) if the
        mounted set has nothing to act on. Shared by the upward sweep and the
        mid-run bottom re-check so both use identical dedup rules."""
        for i, d in enumerate(self._mounted_targets()):   # newest-first
            mid = d.get("mid")
            if mid and mid in processed:
                continue
            if self.watermark.is_handled(self._chat, mid):
                continue
            return d, i
        return None, 0

    # ---- kill switch + "already seen up to here" boundary ---------------
    def _check_stop(self) -> bool:
        """True once the run should abort because you pressed Stop. Sets a sticky
        stop_reason the first time so the runner/UI can say why. This is the
        MANUAL kill switch only — the automatic 'reached older reels' stop is the
        boundary check in iter_reels, which is a normal (silent) end-of-sweep."""
        if self.stop_reason:
            return True
        try:
            if self.should_stop():
                self.stop_reason = "You pressed Stop."
                log.warning("stop button pressed — ending the run")
                return True
        except Exception:
            pass
        return False

    # JS: read the two "I've already seen up to here" markers currently mounted:
    #   * texts       — plain-text bubbles YOU typed (right-aligned, real text, no
    #                   Clip inside), each with its viewport y. The bot never
    #                   sends a bare text (its replies quote a reel), so any such
    #                   bubble is yours.
    #   * reactedMids — media ids of reels wearing a reaction badge (the tiny
    #                   emoji under a reel). The bot's own reactions are filtered
    #                   out in Python via _bot_reacted_mids.
    # The caller turns these into a boundary y (see _boundary_y): a reel at/older
    # than your newest such marker is one you've already seen, so the upward sweep
    # stops there instead of walking on into old reels. If IG changes the
    # reaction-badge markup, the `reacted`/`reaction` aria-label match is the one
    # line to re-verify (same policy as selectors.py).
    _MARKERS_JS = """
    () => {
      const MID_RE = /\\/(\\d{6,})_(\\d{6,})_/;
      const idFromSrc = (src) => {
        src = src || '';
        const m = src.match(MID_RE);
        if (m) return m[1] + '_' + m[2];
        let path = src;
        try { path = new URL(src, location.href).pathname; } catch (e) { path = src.split('?')[0]; }
        let h = 0;
        for (let i = 0; i < path.length; i++) { h = (h * 31 + path.charCodeAt(i)) | 0; }
        return 'p:' + (h >>> 0).toString(36);
      };
      const texts = [];
      for (const el of document.querySelectorAll('div')) {
        let jc = '';
        try { jc = getComputedStyle(el).justifyContent || ''; } catch (e) {}
        if (!(jc === 'flex-end' || jc === 'end' || jc === 'right')) continue;
        if (el.querySelector('[aria-label="Clip"]')) continue;   // reel / reel reply
        const r = el.getBoundingClientRect();
        if (r.height > 400 || r.height < 8) continue;             // skip big wrappers
        const t = (el.innerText || '').trim();
        if (t && /[A-Za-z0-9]/.test(t)) texts.push({t: t, y: r.y});
      }
      const reactedMids = new Set();
      for (const clip of document.querySelectorAll('[aria-label="Clip"]')) {
        let node = clip, mid = null, badge = false;
        for (let up = 0; up < 12 && node; up++) {
          if (!mid && node.querySelector) {
            const imgs = [...node.querySelectorAll('img[src]')];
            const mided = imgs.find(im => MID_RE.test(im.getAttribute('src') || ''));
            const pick = mided || imgs.sort((a, b) => {
              const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
              return rb.width * rb.height - ra.width * ra.height;
            })[0];
            if (pick) mid = idFromSrc(pick.getAttribute('src'));
          }
          if (!badge && node.querySelector) {
            if (node.querySelector('[aria-label*="reaction" i], [aria-label*="reacted" i]'))
              badge = true;
          }
          node = node.parentElement;
        }
        if (mid && badge) reactedMids.add(mid);
      }
      return {texts: texts, reactedMids: [...reactedMids]};
    }
    """

    def _markers(self) -> dict:
        try:
            data = self.page.evaluate(self._MARKERS_JS) or {}
        except Exception:
            return {"texts": [], "reactedMids": []}
        return {"texts": list(data.get("texts") or []),
                "reactedMids": list(data.get("reactedMids") or [])}

    def _boundary_y(self) -> float | None:
        """The viewport y of YOUR most recent "seen up to here" marker currently
        mounted — the newest of (a message you typed, a reel you reacted to
        yourself). Reels NEWER than this (larger y, nearer the composer) are
        unread; anything at or older than it you've already handled, so the sweep
        stops there. None if no such marker is mounted. The bot's own
        replies/reactions are excluded so it never draws the line at itself."""
        m = self._markers()
        ys: list[float] = []
        for item in m["texts"]:
            t = (item.get("t") or "").strip()
            if t and t not in self._bot_sent_texts:
                ys.append(item.get("y", 0))
        manual = [mid for mid in m["reactedMids"]
                  if mid and mid not in self._bot_reacted_mids]
        if manual:
            y_by_mid = {d.get("mid"): d.get("y", 0)
                        for d in self._reel_descriptors()}
            ys += [y_by_mid[mid] for mid in manual if mid in y_by_mid]
        return max(ys) if ys else None

    def _at_seen_boundary(self, desc: dict) -> bool:
        """True if the reel we'd handle next sits at or above (older than) your
        most recent own message / own reaction — i.e. we've climbed out of the
        new reels into ones you've already seen."""
        by = self._boundary_y()
        if by is None:
            return False
        return desc.get("y", 0) <= by

    def iter_reels(self):
        """Yield (ReelHandle, ReelContext) for the newest UNHANDLED target reels.

        The DM message list is virtualized (only ~2-3 reel bubbles mount at
        once), so we can't enumerate the whole thread in one shot. Instead we
        sweep from the bottom (newest) upward with the mouse wheel, and at each
        step pick the newest mounted reel we haven't handled yet — identified by
        its cover-image MEDIA ID, the one stable per-reel handle in the thread
        DOM (see watermark.py). We stop when:
          * we've yielded settings.max_new_reels reels, or
          * we reach a reel already reacted to on a prior run (everything older
            is handled too), or
          * wheeling up stops revealing new reels (top of history reached).

        Media-id dedup means each physical reel is opened+reacted at most once,
        fixing both the old bugs: missing most of a burst (only the rendered two
        were seen) and re-reacting to the same reel (index-based identity drifted
        as bubbles mounted/unmounted).

        Mid-run arrivals: the sender often keeps sharing reels WHILE we react.
        Those land at the bottom, but by then our sweep has scrolled up and the
        virtualized list has unmounted them, so a single bottom->top pass misses
        them and leaves the sender seen-zoned. After the upward sweep stalls we
        therefore return to the bottom and re-check; if new unhandled reels
        appeared we keep going (bounded by `cap`), only stopping once a bottom
        re-check finds nothing new."""
        cap = getattr(self.config.settings, "max_new_reels", 12)
        self._kill_screentime()
        self._scroll_thread_to_bottom()
        self._settle_reels()

        processed: set = set()   # media ids yielded this run (within-run dedup)
        count, stall = 0, 0
        # Consecutive wheel-up steps that surfaced no new reel to act on. We stop
        # once this hits the limit — that's either the top of the thread OR we've
        # scrolled up into fully-handled/older territory (a burst's reels are
        # contiguous, so a run of dead steps means the burst is cleared). Using a
        # stall counter (not "break on the first handled reel") keeps us from
        # cutting a burst short when replying auto-scrolls its older reels
        # temporarily out of view.
        STALL_MAX = 6
        # After the upward sweep is exhausted, how many bottom re-checks we poll
        # for reels that arrived mid-run before concluding there's nothing new.
        # A share takes a few seconds to paint, and reacting to a just-caught
        # arrival (open viewer -> read -> close) can itself outlast a short
        # window — so we poll several times with a real wait between, giving a
        # reel sent up to ~15s after the sweep finished time to appear (verified
        # live: a 2nd mid-run reel sent ~5s after the 1st was missed by a 2-poll
        # window and only caught on the next run). The watermark still guarantees
        # anything sent even later is caught on the following run.
        BOTTOM_RECHECK_MAX = 5
        RECHECK_WAIT_MS = 2200
        while count < cap:
            self._kill_screentime()
            # Manual kill switch (Stop button) — bail immediately.
            if self._check_stop():
                break
            nxt, rank = self._pick_next(processed)

            # Boundary: if the reel we'd open next is at/older than your newest
            # own message or your own reaction on a reel, we've climbed out of the
            # NEW reels into ones you've already seen — stop the sweep here. This
            # is the normal end of a chat (not an alarm): older reels are handled.
            if nxt is not None and self._at_seen_boundary(nxt):
                log.info("reel %s in %r is at/older than your last-seen marker "
                         "(your message / your reaction) — new reels are over; "
                         "stopping the sweep", nxt.get("mid"), self._chat)
                break

            if nxt is None:
                if stall < STALL_MAX:
                    # Nothing to act on in view — wheel up to reveal older reels
                    # and retry. Stop the upward walk after several dead steps.
                    self._wheel(-1200)
                    self._wait(900)
                    stall += 1
                    continue
                # Upward sweep exhausted. Return to the bottom and poll for reels
                # that arrived while we were reacting (else the sender is left
                # seen-zoned). Poll a few times so a very-recent send has time to
                # paint; stop only when a re-check genuinely finds nothing new.
                found = False
                for _ in range(BOTTOM_RECHECK_MAX):
                    self._scroll_thread_to_bottom()
                    self._settle_reels()
                    self._wait(RECHECK_WAIT_MS)
                    if self._check_stop():   # Stop button pressed mid-poll
                        break
                    cand = self._pick_next(processed)[0]
                    # Only a reel NEWER than your last-seen marker counts as a
                    # genuine mid-run arrival; one at/older than it is just old
                    # territory and must not restart the sweep.
                    if cand is not None and not self._at_seen_boundary(cand):
                        found = True
                        break
                if self.stop_reason or not found:
                    break
                stall = 0          # resume clearing from the (new) bottom
                continue

            stall = 0
            mid = nxt.get("mid")
            if mid:
                processed.add(mid)
            # Thumbnail from the DM bubble (crisp cover frame); open+read for
            # comments, caption, and the stable shortcode.
            bubble = self._capture_thumbnail(nxt, count)
            ctx = self._open_and_read_desc(nxt)
            if bubble:
                ctx.thumbnail_path = bubble
            # Best-effort "Nth from the bottom" for the review UI: rank among the
            # reels mounted newest-first when we picked this one (1 = newest).
            ctx.position_from_bottom = rank + 1
            count += 1
            yield ReelHandle(reel_id=ctx.reel_id or mid or f"reel#{count}",
                             locator={"mid": mid, "box": nxt.get("box")}), ctx
        log.info("iter_reels: yielded %d reel(s) in %r", count, self._chat)

    # ---- incoming text from the other person ---------------------------
    # JS: the OTHER person's plain-text messages that arrived since our last
    # reply, each with its bubble rect so the caller can screenshot it. Signals:
    #   * outgoing (ours) = right-aligned (justify-content flex-end / right of the
    #     pane centre). Its newest y is the watermark — anything below it is new.
    #   * incoming (theirs) = a left-of-centre bubble with real text and no Clip
    #     inside (so reels and reel reply-quotes are excluded).
    # Newest-first; deduped by text keeping the tightest bubble. If IG changes its
    # DM markup this is the block to re-verify (same policy as selectors.py).
    _INCOMING_TEXTS_JS = """
    () => {
      const isRight = (el) => {
        let jc = '';
        try { jc = getComputedStyle(el).justifyContent || ''; } catch (e) {}
        return jc === 'flex-end' || jc === 'end' || jc === 'right';
      };
      // Watermark: y of our newest outgoing message (text OR reel reply).
      let wmY = -1;
      for (const el of document.querySelectorAll('div')) {
        if (!isRight(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.height < 16 || r.height > 600) continue;
        const hasText = ((el.innerText || '').trim().length > 0);
        if (!hasText && !el.querySelector('img[src]')) continue;
        if (r.y > wmY) wmY = r.y;
      }
      const pane = document.querySelector('div[role="textbox"][contenteditable="true"]')
                || document.querySelector('div[role="main"]');
      let paneMid = innerWidth / 2, paneW = innerWidth;
      if (pane) { const m = pane.getBoundingClientRect(); paneMid = m.x + m.width / 2; paneW = m.width; }
      const byText = new Map();
      for (const el of document.querySelectorAll('div')) {
        if (isRight(el)) continue;                        // ours
        if (el.querySelector('[aria-label="Clip"]')) continue;   // reel / reel quote
        const t = (el.innerText || '').trim();
        if (!t || !/[A-Za-z0-9]/.test(t)) continue;
        const r = el.getBoundingClientRect();
        if (r.height < 16 || r.height > 300 || r.width < 40) continue;
        const mid = r.x + r.width / 2;
        if (!(mid < paneMid - paneW * 0.08)) continue;    // must sit on the left
        if (wmY >= 0 && r.y <= wmY) continue;             // older than our last reply
        const area = r.width * r.height;
        const prev = byText.get(t);
        if (!prev || area < prev.area)
          byText.set(t, {t: t, x: r.x, y: r.y, w: r.width, h: r.height, area: area});
      }
      const items = [...byText.values()];
      items.sort((a, b) => b.y - a.y);                    // newest first
      return items;
    }
    """

    def unanswered_incoming_texts(self, cap: int = 8) -> list[str]:
        return [t for t, _ in self.incoming_texts_with_shots(cap)]

    def incoming_texts_with_shots(
            self, cap: int = 8) -> list[tuple[str, str | None]]:
        """The other person's unanswered text messages, each with a screenshot of
        the bubble so the review panel can show it like a reel it couldn't handle.
        Called with the thread freshly scrolled to the bottom (open_chat does
        that). Never raises."""
        self._kill_screentime()
        try:
            items = self.page.evaluate(self._INCOMING_TEXTS_JS) or []
        except Exception:
            log.exception("incoming-text scan failed for %r", self._chat)
            return []
        out: list[tuple[str, str | None]] = []
        for i, it in enumerate(items[:cap]):
            t = (it.get("t") or "").strip()
            if not t:
                continue
            shot = self._capture_text_shot(it, i)
            snip = t if len(t) <= 140 else t[:139] + "…"
            out.append((snip, shot))
        log.info("incoming-text scan: %d message(s) in %r", len(out), self._chat)
        return out

    def _capture_text_shot(self, it: dict, idx: int) -> str | None:
        """Screenshot one incoming text bubble (padded slightly). None on any
        failure — the flag then just shows the message text, as before."""
        w, h = it.get("w", 0), it.get("h", 0)
        if w < 8 or h < 8:
            return None
        os.makedirs(self.thumbs_dir, exist_ok=True)
        safe_chat = re.sub(r"[^\w.-]", "_", self._chat)[:40]
        path = os.path.join(self.thumbs_dir,
                            f"{safe_chat}_text_{idx}_{int(time.time())}.png")
        pad = 6
        try:
            self.page.screenshot(path=path, clip={
                "x": max(0, it["x"] - pad), "y": max(0, it["y"] - pad),
                "width": w + pad * 2, "height": h + pad * 2})
            return path
        except Exception:
            log.exception("incoming-text screenshot failed")
            return None

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

    # JS: scroll the reel bubble whose cover-image media id matches into the
    # viewport centre and return its FRESH rect. We identify by media id (not a
    # DOM index) because the index is meaningless under virtualization — the
    # mounted clip list rotates as we scroll. Returns null if no mounted clip
    # currently carries that media id.
    _SCROLL_REEL_BY_MID_JS = """
    (mid) => {
      const MID_RE = /\\/(\\d{6,})_(\\d{6,})_/;
      const idFromSrc = (src) => {
        src = src || '';
        const m = src.match(MID_RE);
        if (m) return m[1] + '_' + m[2];
        let path = src;
        try { path = new URL(src, location.href).pathname; } catch (e) { path = src.split('?')[0]; }
        let h = 0;
        for (let i = 0; i < path.length; i++) { h = (h * 31 + path.charCodeAt(i)) | 0; }
        return 'p:' + (h >>> 0).toString(36);
      };
      const clips = [...document.querySelectorAll('[aria-label="Clip"]')];
      for (const clip of clips) {
        let node = clip, coverImg = null, coverRect = null, portrait = null, found = null;
        for (let up = 0; up < 12 && node; up++) {
          if (!coverImg && node.querySelector) {
            const imgs = [...node.querySelectorAll('img[src]')];
            const mided = imgs.find(im => MID_RE.test(im.getAttribute('src') || ''));
            const pick = mided || imgs.sort((a, b) => {
              const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
              return rb.width * rb.height - ra.width * ra.height;
            })[0];
            if (pick) { coverImg = pick; coverRect = pick.getBoundingClientRect();
                        found = idFromSrc(pick.getAttribute('src')); }
          }
          const r = node.getBoundingClientRect();
          if (!portrait && r.width >= 120 && r.width <= 600 &&
              r.height >= 150 && r.height <= 900 && r.height >= r.width) portrait = node;
          node = node.parentElement;
        }
        if (found !== mid) continue;
        // Scroll the largest real target into view and return the cover rect
        // (the reliable click target) when we have it, else the portrait box.
        const scrollTarget = portrait || (coverImg && coverImg.closest('*')) || clip;
        scrollTarget.scrollIntoView({block: 'center', inline: 'center'});
        if (coverImg) {
          const cr = coverImg.getBoundingClientRect();
          if (cr.width >= 100 && cr.height >= 150)
            return {x: cr.x, y: cr.y, w: cr.width, h: cr.height};
        }
        const r = (portrait || clip).getBoundingClientRect();
        return {x: r.x, y: r.y, w: r.width, h: r.height};
      }
      return null;
    }
    """

    def _fresh_box_for_mid(self, mid: str | None):
        """Scroll the reel with this media id into view and return its fresh box,
        or None if it isn't mounted. No-op fallback keeps the cached box."""
        if not mid:
            return None
        try:
            fresh = self.page.evaluate(self._SCROLL_REEL_BY_MID_JS, mid)
            if fresh and fresh.get("w", 0) > 8 and fresh.get("h", 0) > 8:
                # Let the scroll settle before the caller clicks — a click that
                # races the scroll momentum lands off the cover and the reel
                # mis-flags "unable to read".
                self._wait(650)
                return fresh
        except Exception:
            log.exception("scroll-into-view failed for mid %s", mid)
        return None

    def _try_open_reel(self, mid: str | None, cached_box: dict) -> str:
        """Click a reel bubble open and return its shortcode, or "" if it didn't
        open. Re-resolves the bubble's box by media id right before clicking so
        stale coords (from a post-reply auto-scroll) don't make the click miss."""
        box = self._fresh_box_for_mid(mid) or cached_box or {}
        cx = box.get("x", 0) + box.get("w", 0) / 2
        cy = box.get("y", 0) + box.get("h", 0) / 2
        # A center click on a tall reel bubble lands where IG's hover controls
        # (React/Reply/More) appear and just *reveals* them; clicking the UPPER
        # portion of the cover opens it reliably (verified live). Try upper-third
        # first, then centre.
        upper_y = box.get("y", 0) + box.get("h", 0) * 0.3
        self._kill_screentime()
        self.page.mouse.click(cx, upper_y)
        self._wait(1100)
        sc = self._shortcode_from_url()
        if not sc:
            self.page.mouse.click(cx, cy)
            self._wait(1100)
            sc = self._shortcode_from_url()
        self._snap_live()
        return sc

    def _open_and_read_desc(self, desc: dict) -> ReelContext:
        """Open the reel described by `desc` (identified by media id), read its
        comments/caption/shortcode, then close the viewer. Never raises.

        Retries the open a couple of times: right after we reply to the previous
        reel, Instagram auto-scrolls the thread to the newest message, so the
        next reel we go to open is briefly mid-scroll and the first click can
        land on empty space or a hover control. A short settle + a fresh
        scroll-into-view on retry clears that (it was making ~half a burst
        mis-flag "unable to read")."""
        mid = desc.get("mid")
        tag = mid or "reel"
        try:
            shortcode = ""
            for attempt in range(3):
                if attempt:
                    # let any post-reply auto-scroll finish, then re-resolve.
                    self._wait(700)
                shortcode = self._try_open_reel(mid, desc.get("box") or {})
                if shortcode:
                    break
            if not shortcode:
                log.warning("reel %s did not open to a /p|reel/ url", tag)
                return ReelContext(chat_name=self._chat, reel_id=str(tag),
                                   read_error=True)
            comments, count = collect_comments(
                self.page, limit=self.config.settings.comments_to_read)
            caption = self._capture_caption()
            cover = self._capture_cover(shortcode or tag)
            self._close_reel_viewer()
            return ReelContext(
                chat_name=self._chat, reel_id=shortcode,
                comments=comments, comment_count=count,
                caption=caption, thumbnail_path=cover,
                read_error=(comments == [] and count is None))
        except Exception:
            log.exception("_open_and_read_desc failed for reel %s", tag)
            self._close_reel_viewer()
            return ReelContext(chat_name=self._chat, reel_id=str(tag),
                               read_error=True)

    def _shortcode_from_url(self) -> str:
        m = re.search(S.REEL_URL_RE, self.page.url or "")
        return m.group(1) if m else ""

    def _close_reel_viewer(self) -> None:
        try:
            if re.search(S.REEL_URL_RE, self.page.url or ""):
                self.page.keyboard.press("Escape")
                self._wait(800)
            if re.search(S.REEL_URL_RE, self.page.url or ""):
                # still on the reel — navigate back to the thread explicitly
                self.page.go_back()
                self._wait(800)
        except Exception:
            log.exception("closing reel viewer failed")

    def build_reel_context(self, reel: ReelHandle) -> ReelContext:
        loc = reel.locator if isinstance(reel.locator, dict) else {}
        mid = loc.get("mid")
        desc = self._find_target_by_mid(mid) or {"mid": mid, "box": loc.get("box")}
        return self._open_and_read_desc(desc)

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

    def _find_target_by_mid(self, mid: str | None):
        """The currently-mounted descriptor with this media id, or None."""
        if not mid:
            return None
        return next((d for d in self._mounted_targets() if d.get("mid") == mid),
                    None)

    def _locate_bubble(self, reel: ReelHandle) -> dict | None:
        """Resolve the reel handle to a fresh, scrolled-into-view descriptor for
        a send. Handles come from iter_reels carrying the media id; the reel may
        have unmounted since (viewer close, thread re-render), so re-find and
        re-scroll it into view by media id."""
        loc = reel.locator if isinstance(reel.locator, dict) else {}
        mid = loc.get("mid")
        fresh = self._fresh_box_for_mid(mid)          # scrolls it into view
        desc = self._find_target_by_mid(mid)
        if desc is None and (fresh or loc.get("box")):
            desc = {"mid": mid, "box": fresh or loc.get("box")}
        elif desc is not None and fresh:
            desc = {**desc, "box": fresh}
        return desc

    def _hover_reveal_controls(self, desc: dict) -> None:
        box = desc.get("box") or {}
        cx = box.get("x", 0) + box.get("w", 0) / 2
        cy = box.get("y", 0) + box.get("h", 0) / 2
        self.page.mouse.move(cx, cy)
        self._wait(500)

    # JS: is a reply-quote currently staged? Arming "Reply to message from X"
    # inserts a "Replying to <name>" preview with a "Cancel reply" control above
    # the composer — but in a SEPARATE DOM subtree, not inside the composer's
    # ancestry (verified live), so we look document-wide. The "Cancel reply"
    # control is specific to a staged quote and is the primary signal; the
    # "Replying to" text is a fallback. (If IG changes this wording, this one
    # predicate is the thing to re-verify.)
    _REPLY_PREVIEW_JS = """
    () => {
      for (const el of document.querySelectorAll('[aria-label]')) {
        if (/^cancel reply/i.test(el.getAttribute('aria-label') || '')) {
          const r = el.getBoundingClientRect();
          if (r.width || r.height) return true;
        }
      }
      return /replying to/i.test(document.body.innerText || '');
    }
    """

    # JS: click the hover control matching `prefix` (e.g. "Reply to message
    # from ") that STRUCTURALLY belongs to the reel whose cover-image media id is
    # `mid` — it's a descendant of that message's row. This targets the RIGHT
    # bubble even when several reels are mounted, each with its own
    # React/Reply/More cluster (the old `.last`/nearest-by-coords picks grabbed a
    # neighbouring bubble's control and the click missed). The bubble must be
    # hovered first so the controls are rendered. Returns whether it clicked.
    _CLICK_CONTROL_JS = """
    (args) => {
      const [mid, prefix] = args;
      const MID_RE = /\\/(\\d{6,})_(\\d{6,})_/;
      const idFromSrc = (src) => {
        src = src || '';
        const m = src.match(MID_RE);
        if (m) return m[1] + '_' + m[2];
        let path = src;
        try { path = new URL(src, location.href).pathname; } catch (e) { path = src.split('?')[0]; }
        let h = 0;
        for (let i = 0; i < path.length; i++) { h = (h * 31 + path.charCodeAt(i)) | 0; }
        return 'p:' + (h >>> 0).toString(36);
      };
      const clips = [...document.querySelectorAll('[aria-label="Clip"]')];
      for (const clip of clips) {
        let found = null, node = clip;
        for (let up = 0; up < 12 && node; up++) {
          if (node.querySelector) {
            const imgs = [...node.querySelectorAll('img[src]')];
            const mided = imgs.find(im => MID_RE.test(im.getAttribute('src') || ''));
            const pick = mided || imgs.sort((a, b) => {
              const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
              return rb.width * rb.height - ra.width * ra.height;
            })[0];
            if (pick) { found = idFromSrc(pick.getAttribute('src')); break; }
          }
          node = node.parentElement;
        }
        if (found !== mid) continue;
        node = clip;
        for (let up = 0; up < 12 && node; up++) {
          const btn = node.querySelector && node.querySelector(
            'div[aria-label^="' + prefix + '"]');
          if (btn) { btn.click(); return true; }
          node = node.parentElement;
        }
        return false;
      }
      return false;
    }
    """

    def _click_message_control(self, mid: str | None, box: dict, prefix: str) -> bool:
        """Hover the reel bubble, then click ITS hover control for `prefix`,
        resolved structurally so it targets the right bubble when several are
        mounted. Returns whether it clicked. Never raises."""
        box = box or {}
        cx = box.get("x", 0) + box.get("w", 0) / 2
        cy = box.get("y", 0) + box.get("h", 0) / 2
        try:
            self.page.mouse.move(cx, cy)
            self._wait(500)
            return bool(self.page.evaluate(self._CLICK_CONTROL_JS, [mid, prefix]))
        except Exception:
            log.exception("clicking %r control for reel %s failed", prefix, mid)
            return False

    def _reply_quote_armed(self) -> bool:
        """Whether a reply-quote preview is currently showing above the composer.
        Best-effort; a False here just means we couldn't confirm the tag."""
        try:
            return bool(self.page.evaluate(self._REPLY_PREVIEW_JS))
        except Exception:
            return False

    def _arm_reply_quote(self, desc: dict) -> bool:
        """Click the reel's OWN Reply control so the outgoing message quotes
        (tags) it, VERIFYING the quote preview actually appeared. Retries once on
        the same bubble if the first attempt didn't take. Returns whether the
        quote is armed — the caller sends either way (an untagged reply is better
        than none), but a False is logged so we notice tagging regressions."""
        mid = desc.get("mid")
        box = desc.get("box") or {}
        for attempt in range(2):
            clicked = self._click_message_control(mid, box, S.REPLY_BUTTON_PREFIX)
            if not clicked:
                log.info("reply-quote control not found for reel %s (attempt %d)",
                         mid, attempt + 1)
                continue
            self._wait(500)
            if self._reply_quote_armed():
                return True
        return False

    def send_reaction(self, reel: ReelHandle, emoji: str) -> bool:
        if not self._send_allowed():
            return False
        if _norm_emoji(emoji) not in {_norm_emoji(e) for e in S.QUICK_REACTION_EMOJIS}:
            return False   # not a one-tap emoji; let the runner fall back to reply
        desc = self._locate_bubble(reel)
        mid = (reel.locator or {}).get("mid")
        if desc is None:
            return False
        # Arm the reaction bar FIRST. If we can't even open it, return False
        # before touching anything — the runner then falls back to a typed
        # reply. Once we've actually clicked an emoji we return True regardless,
        # so the runner never ALSO sends a reply on top of a reaction we just
        # placed (that double-send was one way a reel got "reacted to twice").
        try:
            self._hover_reveal_controls(desc)
            react = self.page.locator(
                f'[aria-label^="{S.REACT_BUTTON_PREFIX}"]').last
            react.click()
            self._wait(500)
        except Exception:
            log.info("reaction bar not armed; falling back to typed reply")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        try:
            target = _norm_emoji(emoji)
            btn = self.page.locator('[role="button"]').filter(
                has_text=re.compile(re.escape(target))).last
            btn.click()
            self._wait(600)
        except Exception:
            log.exception("emoji pick failed after opening reaction bar")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
        # Best-effort either way: mark handled so we never re-react to this reel.
        log.info("reacted %s to reel %s in %r", emoji, mid, self._chat)
        self.watermark.mark(self._chat, mid)
        # Remember it's OUR reaction so the "you reacted yourself" detector never
        # trips on the badge the bot just placed.
        if mid:
            self._bot_reacted_mids.add(mid)
        self._snap_live()
        return True

    def send_reply(self, reel: ReelHandle, text: str) -> bool:
        if not self._send_allowed():
            return False
        desc = self._locate_bubble(reel)
        mid = (reel.locator or {}).get("mid")
        try:
            # Quote the specific reel so the reply attaches to (tags) it, not a
            # floating thread message the sender can't map back to a reel. We
            # verify the quote actually took; if it couldn't be armed we still
            # send (an untagged reply beats leaving them on read) but log it.
            tagged = self._arm_reply_quote(desc) if desc is not None else False
            if not tagged:
                log.warning("could not tag reel %s with a reply-quote in %r; "
                            "sending untagged", mid, self._chat)
            composer = self.page.locator(S.COMPOSER).last
            composer.click()
            composer.type(text, delay=25)
            self._wait(300)
            self.page.keyboard.press("Enter")
            self._wait(800)
            log.info("replied %r to reel %s in %r (tagged=%s)",
                     text, mid, self._chat, tagged)
            self.watermark.mark(self._chat, mid)
            # Remember our own outgoing text so the "you typed a message"
            # detector never trips on the reply the bot just sent.
            if text and text.strip():
                self._bot_sent_texts.add(text.strip())
            if mid:
                self._bot_reacted_mids.add(mid)
            self._snap_live()
            return True
        except Exception:
            log.exception("send_reply failed")
            return False

    def close(self) -> None:
        try:
            self.driver.close()
        except Exception:
            pass
