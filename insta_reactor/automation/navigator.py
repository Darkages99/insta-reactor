"""State-machine navigation over Instagram.

Goals in, verified transitions out. Every public method issues a *goal*
("be at the inbox", "open this chat") and internally: acts, verifies the
resulting state, retries on failure, and recovers to a known state if it gets
lost. Nothing here assumes a tap succeeded.
"""

from __future__ import annotations

import logging
import time

from ..device.base import Device, UiNode
from . import selectors as S
from .states import State, INSTAGRAM_PACKAGE, MAX_BACK_TO_INBOX

log = logging.getLogger("insta_reactor.navigator")


class NavigationError(RuntimeError):
    pass


class Navigator:
    def __init__(self, device: Device, settle: float = 0.8):
        self.d = device
        self.settle = settle   # seconds to wait for the UI to settle after an action
        # Last screen coordinate the comments button was found at via the
        # normal selector match — reel-viewer chrome is at a fixed position
        # regardless of the reel's content, so this survives across reels
        # and backs up open_comments() when the icon is undetectable
        # (e.g. a white icon on a white-background reel).
        self.comments_button_center: tuple[int, int] | None = None

    # ---- low-level matcher helpers --------------------------------------
    def _find_any(self, candidates: list[dict]) -> UiNode | None:
        for matcher in candidates:
            node = self.d.find(**matcher)
            if node:
                return node
        return None

    def _exists_any(self, candidates: list[dict]) -> bool:
        return self._find_any(candidates) is not None

    def _pause(self, factor: float = 1.0) -> None:
        time.sleep(self.settle * factor)

    # ---- state detection -------------------------------------------------
    def detect_state(self) -> State:
        if self.d.current_package() != INSTAGRAM_PACKAGE:
            return State.NOT_INSTAGRAM
        hit = self._find_any(S.DISMISS_INTERRUPTION)
        if hit:
            log.info("detect_state: INTERRUPTION — matched text=%r desc=%r id=%r",
                      hit.text, hit.desc, hit.resource_id)
            return State.INTERRUPTION
        # Order matters: comments sheet sits on top of the reel viewer.
        hit = self._find_any(S.STATE_FINGERPRINTS["COMMENTS"])
        if hit:
            log.info("detect_state: COMMENTS — matched text=%r desc=%r id=%r",
                      hit.text, hit.desc, hit.resource_id)
            return State.COMMENTS
        hit = self._find_any(S.STATE_FINGERPRINTS["REEL_VIEWER"])
        if hit:
            log.info("detect_state: REEL_VIEWER — matched text=%r desc=%r id=%r",
                      hit.text, hit.desc, hit.resource_id)
            return State.REEL_VIEWER
        hit = self._find_any(S.STATE_FINGERPRINTS["CHAT"])
        if hit:
            log.info("detect_state: CHAT — matched text=%r desc=%r id=%r",
                      hit.text, hit.desc, hit.resource_id)
            return State.CHAT
        if self._exists_any(S.STATE_FINGERPRINTS["INBOX"]):
            return State.INBOX
        nodes = self.d.find_all()
        ids = sorted({n.resource_id for n in nodes if n.resource_id})
        texts = [n.text for n in nodes if n.text][:15]
        descs = [n.desc for n in nodes if n.desc][:15]
        log.info("detect_state: UNKNOWN — %d nodes, resource-ids=%s texts=%s descs=%s",
                 len(nodes), ids, texts, descs)
        return State.UNKNOWN

    def wait_for_state(self, target: State, timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.detect_state()
            if st == State.INTERRUPTION:
                self.dismiss_interruptions()
                continue
            if st == target:
                return True
            self._pause(0.4)
        return False

    # ---- interruption / recovery ----------------------------------------
    def dismiss_interruptions(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            node = self._find_any(S.DISMISS_INTERRUPTION)
            if not node:
                return
            log.info("dismissing interruption: %r/%r", node.text, node.desc)
            self.d.tap_node(node)
            self._pause()

    def recover_to_inbox(self) -> bool:
        """Panic path: dismiss junk, then reach the inbox.

        The inbox lives behind a bottom-nav tab, not "back" (e.g. from the
        Home feed, Back exits toward the launcher, never toward Direct). So
        prefer tapping the nav tab whenever it's visible, and only fall back
        to Back when it isn't (nested sheets/dialogs hide the tab bar).
        """
        self.dismiss_interruptions()
        if self.d.current_package() != INSTAGRAM_PACKAGE:
            self.d.start_app(INSTAGRAM_PACKAGE)
            self._pause(2.0)
        for _ in range(MAX_BACK_TO_INBOX):
            if self.detect_state() == State.INBOX:
                return True
            tab = self._find_any(S.INBOX_TAB_BUTTON)
            if tab:
                self.d.tap_node(tab)
                # Tapping the tab can trigger a heavier screen transition
                # (thread list fetch/render) than a simple Back press, so
                # give it a real wait instead of a fixed short pause.
                if self.wait_for_state(State.INBOX, timeout=4.0):
                    return True
            else:
                self.d.press_back()
                self._pause()
            self.dismiss_interruptions()
        return self.detect_state() == State.INBOX

    # ---- goals -----------------------------------------------------------
    def ensure_app_open(self) -> None:
        if self.d.current_package() != INSTAGRAM_PACKAGE:
            self.d.start_app(INSTAGRAM_PACKAGE)
            self._pause(2.5)
        self.dismiss_interruptions()

    def ensure_inbox(self, attempts: int = 3) -> None:
        self.ensure_app_open()
        for _ in range(attempts):
            if self.detect_state() == State.INBOX:
                return
            if not self.recover_to_inbox():
                self._pause()
        if self.detect_state() != State.INBOX:
            raise NavigationError("could not reach inbox")

    def open_chat(self, chat_name: str, attempts: int = 2) -> bool:
        """Open a conversation by its display name.

        Scrolls the inbox thread list top-to-bottom looking for the row —
        NOT just whatever happens to be on screen when we arrive. A thread
        buried below the fold is a normal, expected case (most real inboxes
        have more threads than fit one screen), not a fallback-worthy one.
        """
        for attempt in range(attempts):
            self.ensure_inbox()
            row = self._find_inbox_row(chat_name)
            if row:
                log.info("open_chat %r: row visible without scrolling", chat_name)
            else:
                log.info("open_chat %r: not immediately visible, scanning inbox…",
                          chat_name)
                row = self._scan_inbox_for_row(chat_name)
            if not row:
                log.info("open_chat %r: row not found (attempt %d/%d)",
                          chat_name, attempt + 1, attempts)
                self._pause()
                continue
            self.d.tap_node(row)
            if self.wait_for_state(State.CHAT):
                opened = self._current_chat_title()
                if opened is not None and opened != chat_name:
                    # Opened the wrong thread (e.g. a name that merely contains
                    # the target). Back out and try again rather than operate on
                    # the wrong account.
                    log.info("open_chat %r: opened wrong thread %r — backing out "
                              "(attempt %d/%d)", chat_name, opened,
                              attempt + 1, attempts)
                    self.d.press_back()
                    self._pause()
                    continue
                log.info("open_chat %r: reached State.CHAT", chat_name)
                return True
            log.info("open_chat %r: tapped row but never reached State.CHAT "
                      "(attempt %d/%d)", chat_name, attempt + 1, attempts)
            self.d.press_back()
            self._pause()
        return False

    def _current_chat_title(self) -> str | None:
        """The open DM thread's header title (account name), or None if we
        can't read it. Used to confirm we opened the intended chat."""
        node = self._find_any(S.THREAD_TITLE)
        if node is None:
            return None
        return (node.text or node.desc or "").strip() or None

    def _find_inbox_row(self, chat_name: str) -> UiNode | None:
        """Locate `chat_name`'s inbox row by EXACT username — never a substring.

        A substring match is dangerous: targeting "shyam" must not open
        "40fitandshyam" (confirmed live — a descContains fallback did exactly
        that). So we match the bare username TextView by exact text, and for the
        container fallback we parse the row's content-desc ("<name>, <preview>,
        <time>") and require its first field to equal `chat_name` exactly.
        """
        exact = (self.d.find(resource_id="com.instagram.android:id/row_inbox_username",
                             text=chat_name)
                 or self.d.find(text=chat_name))
        if exact:
            return exact
        for n in self.d.find_all(className="android.view.View", descContains=", "):
            name = (n.desc or "").split(",", 1)[0].strip()
            if name == chat_name:
                return n
        return None

    def _inbox_row_signature(self) -> tuple:
        """Cheap fingerprint of the visible inbox rows, to detect when
        scrolling stops making progress (reached the bottom of the list).

        Tries each INBOX_ROW selector and fingerprints the first that matches
        anything — the old `row_inbox_username` id matches nothing on current IG
        builds, which made this always-empty and false-tripped 'end of list' on
        the very first scan step (so rows further down, like 'shyam', were never
        reached). Row containers are keyed by their content-desc, which changes
        as the list scrolls and stabilizes only at the true bottom."""
        for matcher in S.INBOX_ROW:
            nodes = self.d.find_all(**matcher)
            if nodes:
                return tuple((n.text or n.desc, n.bounds[1]) for n in nodes)
        return ()

    def scroll_inbox_to_top(self, max_swipes: int = 10) -> None:
        """Scroll the inbox thread list up to the newest (top) thread."""
        w, h = self.d.window_size()
        last = None
        for _ in range(max_swipes):
            sig = self._inbox_row_signature()
            if sig == last:
                return
            last = sig
            self.d.swipe(w // 2, int(h * 0.30), w // 2, int(h * 0.80), 0.30)
            self._pause(0.5)

    def _scan_inbox_for_row(self, chat_name: str, max_swipes: int = 20) -> UiNode | None:
        """Scroll the inbox downward looking for `chat_name`'s row.

        Re-anchors at the top first so this is deterministic regardless of
        wherever the list happened to be scrolled to already, then walks
        down one screen at a time until the row appears or the list stops
        advancing (we've reached the bottom without finding it).
        """
        self.scroll_inbox_to_top()
        w, h = self.d.window_size()
        last = None
        for step in range(max_swipes):
            row = self._find_inbox_row(chat_name)
            if row:
                log.info("_scan_inbox_for_row %r: found after %d scroll(s)",
                          chat_name, step)
                return row
            sig = self._inbox_row_signature()
            if sig == last:
                log.info("_scan_inbox_for_row %r: reached end of list after "
                          "%d scroll(s), not found", chat_name, step)
                return None
            last = sig
            self.d.swipe(w // 2, int(h * 0.75), w // 2, int(h * 0.30), 0.30)
            self._pause(0.5)
        log.info("_scan_inbox_for_row %r: hit max_swipes=%d without finding it",
                  chat_name, max_swipes)
        return None

    def open_comments(self) -> bool:
        btn = self._find_any(S.OPEN_COMMENTS_BUTTON)
        if btn:
            self.d.tap_node(btn)
            ok = self.wait_for_state(State.COMMENTS)
            log.info("open_comments: button found by selector at %s, tap+wait "
                      "-> COMMENTS=%s", btn.center, ok)
            if ok:
                self.comments_button_center = btn.center
            return ok
        if self.comments_button_center is not None:
            # Some reels (bright/white-background posts) render the comment
            # icon in a color that blends into the background and the
            # selector can't find it — confirmed live. The button's screen
            # position is fixed reel-viewer chrome regardless of the reel's
            # content, so reuse the last coordinate we found it at.
            log.warning("open_comments: button not found by selector (icon "
                        "may blend into a light-background reel) — tapping "
                        "last known chrome coordinate %s",
                        self.comments_button_center)
            self.d.tap(*self.comments_button_center)
            return self.wait_for_state(State.COMMENTS)
        return False

    def close_comments(self) -> bool:
        self.d.press_back()
        return self.wait_for_state(State.REEL_VIEWER, timeout=4.0)

    def back_to_chat(self, attempts: int = 3) -> bool:
        for _ in range(attempts):
            st = self.detect_state()
            if st == State.CHAT:
                return True
            if st == State.INTERRUPTION:
                self.dismiss_interruptions()
                continue
            self.d.press_back()
            self._pause()
        return self.detect_state() == State.CHAT

    def send_reply_text(self, text: str) -> bool:
        composer = self._find_any(S.CHAT_COMPOSER)
        if not composer:
            return False
        self.d.tap_node(composer)
        self._pause(0.5)
        self.d.input_text(text)
        self._pause(0.5)
        send = self._find_any(S.CHAT_SEND_BUTTON)
        if not send:
            return False
        self.d.tap_node(send)
        self._pause()
        return True

    def react_to_reel(self, node: UiNode, text: str) -> bool:
        """React to a reel *from the thread* by swiping right on its bubble.

        In Instagram DMs, a short left→right swipe on a message opens the
        composer with that message quoted ("Replying to …"). Sending from there
        attaches the reply to that specific reel — exactly the reaction we want.
        Assumes we are in the CHAT state with `node` (the reel bubble) on screen.
        """
        if self.detect_state() != State.CHAT:
            return False
        w, h = self.d.window_size()
        l, t, r, b = node.bounds
        y = max(int(h * 0.16), min(int(h * 0.84), (t + b) // 2))
        # start on the bubble, drag rightward ~half the screen to trigger reply.
        x1 = max(l + 20, int(w * 0.18))
        x2 = min(int(w * 0.88), x1 + int(w * 0.50))
        self.d.swipe(x1, y, x2, y, 0.18)
        self._pause()

        # Soft confirmation that the quote chip appeared (don't hard-fail on it).
        if not self._exists_any(S.REPLY_QUOTE_INDICATOR):
            log.info("swipe-to-reply quote chip not detected; trying composer anyway")

        composer = self._find_any(S.CHAT_COMPOSER)
        if not composer:
            return False
        self.d.tap_node(composer)
        self._pause(0.5)
        self.d.input_text(text)
        self._pause(0.6)
        send = self._find_any(S.CHAT_SEND_BUTTON)
        if not send:
            return False
        self.d.tap_node(send)
        self._pause()
        return True

    def reply_in_reel_viewer(self, text: str) -> bool:
        """Reply to the reel currently open in the reel viewer.

        Types into the reel-viewer's own reply bar ("Reply to <name>"), which
        attaches the reply to *this* reel, then taps Send. Assumes we are in the
        REEL_VIEWER state. The Send button only materializes after text exists,
        so we type first, then look for it.
        """
        bar = self._find_any(S.REEL_REPLY_BAR)
        if not bar:
            return False
        self.d.tap_node(bar)
        self._pause(0.5)
        self.d.input_text(text)
        self._pause(0.6)
        send = self._find_any(S.CHAT_SEND_BUTTON)
        if not send:
            return False
        self.d.tap_node(send)
        self._pause()
        return True

    # ---- thread scrolling ------------------------------------------------
    def _thread_signature(self) -> tuple:
        """A cheap fingerprint of the visible thread, to detect when a scroll
        stops making progress (i.e. we've hit the top or bottom).

        Must include reel bubbles, not just text: consecutive reels can scroll
        past with no nearby text changing at all (e.g. back-to-back reel
        shares), which made a text-only signature repeat while a whole new
        reel had actually scrolled into view — stalling scroll_thread_to_bottom
        one screen short of the real bottom (missed the newest reel, live)."""
        tvs = self.d.find_all(className="android.widget.TextView")[:8]
        reels = self.d.find_all(
            resource_id="com.instagram.android:id/reel_share_item_view")
        return (tuple((tv.text, tv.bounds[1]) for tv in tvs),
                tuple(r.bounds for r in reels))

    def scroll_thread_to_top(self, max_swipes: int = 18) -> None:
        """Scroll the open thread all the way up to the oldest message."""
        w, h = self.d.window_size()
        last = None
        for _ in range(max_swipes):
            sig = self._thread_signature()
            if sig == last:
                return
            last = sig
            # drag content downward -> reveals older messages above
            self.d.swipe(w // 2, int(h * 0.28), w // 2, int(h * 0.84), 0.30)
            self._pause(0.6)

    # ---- vanish / disappearing mode guard --------------------------------
    def detect_vanish_mode(self) -> bool:
        """True if Instagram's vanish / disappearing-messages mode is engaged
        (or being pulled into). See S.VANISH_MODE_INDICATORS. Cheap on-screen
        text check; used to abort scrolling before an over-scroll fully engages
        vanish mode, which would otherwise send our reactions as disappearing
        messages and trap the scroll loop in an unsettling pull animation."""
        return self._exists_any(S.VANISH_MODE_INDICATORS)

    def exit_vanish_mode(self) -> bool:
        """Best-effort: get back out of vanish/disappearing mode.

        Leaving the thread turns vanish mode off (its messages disappear on
        exit), so we first try to release any in-progress over-scroll pull with
        a downward swipe, then press Back to leave the thread if it's still
        engaged. The caller is expected to re-open the thread (in normal mode)
        afterwards. Returns True if the vanish indicators are gone.
        """
        if not self.detect_vanish_mode():
            return True
        log.warning("vanish mode detected — attempting to back out")
        w, h = self.d.window_size()
        # reverse an over-scroll pull: drag content back DOWN (toward older),
        # the opposite of the up-pull that engages vanish mode.
        self.d.swipe(w // 2, int(h * 0.35), w // 2, int(h * 0.78), 0.30)
        self._pause(0.6)
        if not self.detect_vanish_mode():
            log.info("vanish mode released by reverse swipe")
            return True
        self.d.press_back()
        self._pause()
        gone = not self.detect_vanish_mode()
        log.info("vanish mode after Back: %s", "cleared" if gone else "STILL ON")
        return gone

    def scroll_thread_to_bottom(self, max_swipes: int = 6) -> None:
        """Gently scroll the open thread down to the newest message.

        IMPORTANT: Instagram activates *vanish mode* when you over-scroll (pull
        up) past the newest message at the very bottom of a thread. A large
        swipe that starts at the bottom edge triggers that pull — and the pull
        animation keeps changing the screen, so a naive "stop when the screen
        stops moving" loop never stops and fully engages vanish mode. To stay
        safe we (a) use short, mid-screen swipes that never start at the bottom
        edge, (b) stop the instant the thread stops advancing, and (c) check for
        vanish mode after each swipe and immediately back out of it if the pull
        started to engage. A freshly opened DM thread already sits at the newest
        message, so usually no swipe is needed at all.
        """
        w, h = self.d.window_size()
        last = None
        for _ in range(max_swipes):
            if self.detect_vanish_mode():
                log.warning("scroll_thread_to_bottom: vanish mode engaging — "
                            "aborting scroll and backing out")
                self.exit_vanish_mode()
                return
            sig = self._thread_signature()
            if sig == last:
                return
            last = sig
            # short mid-screen drag up -> reveals slightly newer content while
            # staying clear of the bottom-edge over-scroll (vanish-mode) zone.
            self.d.swipe(w // 2, int(h * 0.58), w // 2, int(h * 0.44), 0.30)
            self._pause(0.6)
        # one final check: the last swipe itself may have tipped into vanish mode.
        if self.detect_vanish_mode():
            log.warning("scroll_thread_to_bottom: vanish mode after final swipe "
                        "— backing out")
            self.exit_vanish_mode()

    def thread_scroll_down(self, amount: float = 0.5) -> bool:
        """Scroll the thread toward newer messages by ~`amount` of the screen.
        Returns False if nothing changed (we're already at the bottom)."""
        w, h = self.d.window_size()
        before = self._thread_signature()
        y1 = int(h * (0.30 + amount))
        y1 = min(y1, int(h * 0.84))
        self.d.swipe(w // 2, y1, w // 2, int(h * 0.30), 0.30)
        self._pause(0.6)
        return self._thread_signature() != before

    def thread_scroll_up(self, amount: float = 0.5) -> bool:
        """Scroll the thread toward older messages by ~`amount` of the screen.
        Returns False if nothing changed (we're already at the top)."""
        w, h = self.d.window_size()
        before = self._thread_signature()
        y2 = int(h * (0.30 + amount))
        y2 = min(y2, int(h * 0.84))
        self.d.swipe(w // 2, int(h * 0.30), w // 2, y2, 0.30)
        self._pause(0.6)
        return self._thread_signature() != before

    def scroll_node_above_fold(self, top: int, zone_top: int) -> None:
        """Scroll the thread up (toward newer) just enough to push the element
        whose current top is `top` above `zone_top`, so it's no longer a
        candidate. Used to advance the reel-enumeration cursor deterministically.
        """
        w, h = self.d.window_size()
        # move content up by (top - zone_top) + a small margin
        distance = max(int(h * 0.12), (top - zone_top) + int(h * 0.10))
        y_start = min(int(h * 0.80), zone_top + distance)
        self.d.swipe(w // 2, y_start, w // 2, zone_top, 0.30)
        self._pause(0.6)

    def scroll_node_below_fold(self, bottom: int, zone_bottom: int) -> None:
        """Scroll the thread down (toward older) just enough to push the element
        whose current bottom edge is `bottom` below `zone_bottom`, so it's no
        longer a candidate. Advances the newest-first (from-bottom) reel cursor
        deterministically — the mirror of scroll_node_above_fold.
        """
        w, h = self.d.window_size()
        # move content down by (zone_bottom - bottom) + a margin (min 18% screen)
        distance = max(int(h * 0.18), (zone_bottom - bottom) + int(h * 0.10))
        y_start = max(int(h * 0.16), zone_bottom - distance)
        self.d.swipe(w // 2, y_start, w // 2, zone_bottom, 0.30)
        self._pause(0.6)
