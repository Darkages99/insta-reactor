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
        if self._exists_any(S.DISMISS_INTERRUPTION):
            return State.INTERRUPTION
        # Order matters: comments sheet sits on top of the reel viewer.
        if self._exists_any(S.STATE_FINGERPRINTS["COMMENTS"]):
            return State.COMMENTS
        if self._exists_any(S.STATE_FINGERPRINTS["REEL_VIEWER"]):
            return State.REEL_VIEWER
        if self._exists_any(S.STATE_FINGERPRINTS["CHAT"]):
            return State.CHAT
        if self._exists_any(S.STATE_FINGERPRINTS["INBOX"]):
            return State.INBOX
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
        """Open a conversation by its display name."""
        for _ in range(attempts):
            self.ensure_inbox()
            # Prefer tapping the thread row directly if it's already visible
            # in the inbox list — always opens the DM thread. The top search
            # bar is IG's universal ("Ask Meta AI") search, whose result rows
            # open the account's *profile*, not the thread, so it's only a
            # fallback for chats buried too far down to be on-screen.
            row = (self.d.find(resource_id="com.instagram.android:id/row_inbox_username",
                                text=chat_name)
                   or self.d.find(text=chat_name))
            if row:
                self.d.tap_node(row)
                if self.wait_for_state(State.CHAT):
                    return True
                self.d.press_back()
                self._pause()
                continue

            search = self._find_any(S.INBOX_SEARCH)
            if not search:
                self._pause()
                continue
            self.d.tap_node(search)
            self._pause()
            self.d.input_text(chat_name)
            self._pause(1.2)
            result = self.d.find(text=chat_name) or self.d.find(textContains=chat_name)
            if result:
                self.d.tap_node(result)
                if self.wait_for_state(State.CHAT):
                    return True
            # not found -> back out and retry
            self.d.press_back()
            self._pause()
        return False

    def open_comments(self) -> bool:
        btn = self._find_any(S.OPEN_COMMENTS_BUTTON)
        if not btn:
            return False
        self.d.tap_node(btn)
        return self.wait_for_state(State.COMMENTS)

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
        stops making progress (i.e. we've hit the top or bottom)."""
        tvs = self.d.find_all(className="android.widget.TextView")[:8]
        return tuple((tv.text, tv.bounds[1]) for tv in tvs)

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
