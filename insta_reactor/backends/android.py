"""Real-device backend: composes Device + Navigator + CommentCollector.

This is the only place that touches Instagram for real. It implements the
`Backend` interface the runner expects, but internally keeps the modules
separate (navigation vs. collection vs. selectors) so any one can be swapped.

Because Instagram exposes no stable API, the reel/preceding-text detection here
is heuristic and MUST be calibrated against your app version (see
automation/selectors.py). Everything is wrapped so that when observation fails,
we return a ReelContext with read_error=True and the engine flags it for you —
we never crash the run or guess.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from .base import Backend, ReelHandle
from ..config import AppConfig
from ..models import ReelContext, Comment
from ..device.u2_device import U2Device
from ..device.base import UiNode
from ..automation.navigator import Navigator, NavigationError
from ..automation import selectors as S
from ..automation.states import State
from ..collector.comments import CommentCollector

log = logging.getLogger("insta_reactor.android")

# Safe vertical zone (as fractions of screen height) in which a reel bubble is
# fully tappable inside a thread — below the header/back bar and above the
# reply composer. A reel is only opened when it sits entirely within this band.
_ZONE_TOP = 0.16
_ZONE_BOTTOM = 0.82
_MAX_SWEEP_STEPS = 60   # hard cap on scroll+scan iterations per reel lookup
_MAX_REELS = 40         # absolute cap on reels handled per thread (safety)


class AndroidBackend(Backend):
    def __init__(self, config: AppConfig, device=None):
        self.config = config
        self.d = device or U2Device(config.device_serial)
        self.nav = Navigator(self.d)
        self.collector = CommentCollector(self.d)
        self._chat = ""

    # ---- Backend interface ----------------------------------------------
    def prepare(self) -> None:
        self.nav.ensure_inbox()

    def open_chat(self, chat_name: str) -> bool:
        try:
            if self.nav.open_chat(chat_name):
                self._chat = chat_name
                return True
            return False
        except NavigationError:
            log.exception("open_chat failed")
            return False

    def iter_reels(self) -> Iterator[tuple[ReelHandle, ReelContext]]:
        """Yield only the newest (unread) reels, newest-first.

        We do NOT sweep the whole thread history — that would re-scan reels the
        user has already seen and reacted to. Instead we anchor at the *bottom*
        of the thread and walk upward, handling at most `max_new_reels` received
        reels. Instagram exposes no reliable per-message read flag, so this
        newest-N cap is the practical proxy for "unread".

        Reels have no stable id, so we identify each by its *ordinal from the
        bottom* — a stable key within a run, because reading a reel adds no new
        incoming reel bubble (our replies are outgoing and never counted). For
        reel #k we re-anchor at the bottom and skip the k newer reels already
        handled. Reading a reel (open → comments → back) does NOT reliably
        restore scroll position, so re-anchoring from a fixed edge each time is
        what survived testing against the live app.

        Each yielded reel is left open in the reel viewer (unless it was
        short-circuited by preceding text); the runner then calls send_reply
        (which replies from the viewer, tagging the reel) or discard_reel.
        """
        max_new = getattr(self.config.settings, "max_new_reels", 3)
        limit = min(max_new, _MAX_REELS)
        k = 0
        while k < limit:
            node = self._locate_nth_reel_from_bottom(k)
            if node is None:
                log.info("no new reel #%d from bottom — handled %d new reel(s)",
                         k, k)
                return
            # Revisit guard: if your reply already sits right below this reel,
            # it's handled — skip it and walk to older, still-unanswered reels
            # instead of re-opening and re-reacting. (Cross-run dedup by comment
            # signature is a second safety net in the runner.)
            if self._has_outgoing_reply_after(node):
                log.info("new reel #%d already answered — skipping", k)
                k += 1
                continue
            reel_id = f"newreel#{k}"
            log.info("located new reel #%d from bottom (top=%d), reading…",
                     k, node.bounds[1])
            yield ReelHandle(reel_id=reel_id, locator=node), \
                self._read_reel(node, reel_id)
            k += 1

    def find_unreacted_reels(self) -> list[ReelHandle]:
        """Interface compatibility only — the runner uses iter_reels() for the
        real device. Returns reels currently fully visible in the thread."""
        w, h = self.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
        return [ReelHandle(reel_id=f"reel@{n.bounds[0]},{n.bounds[1]}", locator=n)
                for n in self._fully_visible_reels(zt, zb)]

    def build_reel_context(self, reel: ReelHandle) -> ReelContext:
        """Open the reel, read comments, and return to the chat.

        Kept for the abstract interface. iter_reels() uses _read_reel() instead
        (which leaves the reel viewer open so the reply can be tagged)."""
        node = reel.locator
        if not isinstance(node, UiNode):
            return ReelContext(chat_name=self._chat, reel_id=reel.reel_id,
                               read_error=True)
        # _read_reel already returns us to the thread when it finishes.
        return self._read_reel(node, reel.reel_id)

    def send_reply(self, reel: ReelHandle, text: str) -> bool:
        """React to the reel by swiping right on its bubble in the thread.

        After `_read_reel` we're back in the thread with the reel bubble at its
        original position (opening a reel preserves scroll). Swiping right on it
        quotes the reel in the composer, so the reply is attached to *this* reel.
        """
        node = reel.locator if isinstance(reel.locator, UiNode) else None
        if node is None:
            return False
        if self.nav.detect_state() != State.CHAT and not self.nav.back_to_chat():
            return False
        target = self._relocate_reel_node(node) or node
        return self.nav.react_to_reel(target, text)

    def discard_reel(self, reel: ReelHandle) -> None:
        """Return to the thread so the sweep can continue. `_read_reel` already
        leaves us in the thread; this is a defensive no-op-ish safety net."""
        if self.nav.detect_state() != State.CHAT:
            self.nav.back_to_chat()

    def return_to_inbox(self) -> None:
        try:
            self.nav.ensure_inbox()
        except NavigationError:
            self.nav.recover_to_inbox()

    def close(self) -> None:
        pass

    # ---- reel enumeration ------------------------------------------------
    def _fully_visible_reels(self, zone_top: int, zone_bot: int) -> list[UiNode]:
        """Incoming reel bubbles wholly inside the tappable zone, top-first."""
        w, _ = self.d.window_size()
        out: list[UiNode] = []
        for n in self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view"):
            cx, _cy = n.center
            l, t, r, b = n.bounds
            if cx < w / 2 and t >= zone_top and b <= zone_bot:
                out.append(n)
        return sorted(out, key=lambda n: n.bounds[1])

    def _loosely_visible_reels(self, zone_top: int, zone_bot: int) -> list[UiNode]:
        """Incoming reels whose tap target is safely on-screen even if the reel
        is clipped by the composer. Used only at the very bottom of the thread,
        where the newest reel may never fully fit the zone."""
        w, _ = self.d.window_size()
        out: list[UiNode] = []
        for n in self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view"):
            cx, cy = n.center
            if cx < w / 2 and n.bounds[1] >= zone_top and cy <= zone_bot:
                out.append(n)
        return sorted(out, key=lambda n: n.bounds[1])

    def _anchor_chat_top(self) -> bool:
        """Return to the open thread and scroll it to the oldest message — the
        fixed anchor reel enumeration counts from. Reading a reel can leave us
        in the reel viewer or a lost (UNKNOWN) state, so if a plain Back doesn't
        reach the thread we rebuild it by re-opening the chat from the inbox."""
        if self.nav.detect_state() != State.CHAT and not self.nav.back_to_chat():
            try:
                self.nav.recover_to_inbox()
                self.nav.open_chat(self._chat)
            except NavigationError:
                return False
        if self.nav.detect_state() != State.CHAT:
            return False
        self.nav.scroll_thread_to_top()
        return True

    def _anchor_chat_bottom(self) -> bool:
        """Return to the open thread and scroll it to the newest message — the
        fixed anchor that newest-first reel enumeration counts up from. Mirrors
        _anchor_chat_top; rebuilds the thread from the inbox if we got lost."""
        if self.nav.detect_state() != State.CHAT and not self.nav.back_to_chat():
            try:
                self.nav.recover_to_inbox()
                self.nav.open_chat(self._chat)
            except NavigationError:
                return False
        if self.nav.detect_state() != State.CHAT:
            return False
        self.nav.scroll_thread_to_bottom()
        return True

    def _locate_nth_reel_from_bottom(self, k: int) -> UiNode | None:
        """Scroll from the bottom and return the (k)-th incoming reel counting
        from the newest (0-based), or None if there are fewer than k+1 reels."""
        if not self._anchor_chat_bottom():
            return None
        w, h = self.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)

        skipped = 0
        for _ in range(_MAX_SWEEP_STEPS):
            reels = self._loosely_visible_reels(zt, zb)
            if reels:
                newest = reels[-1]          # largest y => closest to bottom
                if skipped == k:
                    return newest
                # advance the cursor: push this reel below the fold so the next
                # scan's bottom-most reel is the preceding (older) one.
                before_bottom = newest.bounds[3]
                self.nav.scroll_node_below_fold(before_bottom, zb)
                skipped += 1
                continue
            # nothing visible in the band — reveal older messages above.
            if not self.nav.thread_scroll_up():
                # at the very top: count any remaining fully-visible reels,
                # newest (bottom-most) first.
                for n in reversed(self._fully_visible_reels(zt, zb)):
                    if skipped == k:
                        return n
                    skipped += 1
                return None   # genuinely fewer than k+1 reels
        return None

    def _locate_nth_reel(self, k: int) -> UiNode | None:
        """Scroll from the top and return the (k)-th incoming reel (0-based),
        or None if there are fewer than k+1 reels in the thread."""
        if not self._anchor_chat_top():
            return None
        w, h = self.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)

        skipped = 0
        for _ in range(_MAX_SWEEP_STEPS):
            reels = self._fully_visible_reels(zt, zb)
            if reels:
                top = reels[0]
                if skipped == k:
                    return top
                # advance the cursor: push this reel above the fold so the next
                # scan's top-most reel is the following one.
                before_top = top.bounds[1]
                self.nav.scroll_node_above_fold(before_top, zt)
                skipped += 1
                continue
            # nothing fully visible here — reveal more below.
            if not self.nav.thread_scroll_down():
                # at the bottom: the newest reel(s) may be clipped by the
                # composer and never fully fit the zone. Count the remaining
                # loosely-visible ones (already-skipped reels are above the fold
                # and excluded by the top >= zone_top test).
                for n in self._loosely_visible_reels(zt, zb):
                    if skipped == k:
                        return n
                    skipped += 1
                return None   # genuinely fewer than k+1 reels
        return None

    def _read_reel(self, node: UiNode, reel_id: str) -> ReelContext:
        """Open `node`, apply Rule 1, read comments, then RETURN TO THE THREAD.

        We react by swiping right on the reel bubble in the thread (not from the
        viewer), so this always leaves us back in the CHAT state with the bubble
        available. Opening a reel preserves the thread's scroll position, so the
        original `node` bounds stay valid for the subsequent swipe-to-react."""
        try:
            preceding = self._preceding_text_for_node(node)
            if preceding:
                # Rule 1: don't automate a reel that has context text above it.
                return ReelContext(
                    chat_name=self._chat, reel_id=reel_id,
                    has_preceding_text=True, preceding_text=preceding,
                    comments=[], comment_count=0,
                )

            self.d.tap_node(node)
            if not self.nav.wait_for_state(State.REEL_VIEWER):
                self.nav.back_to_chat()
                return ReelContext(chat_name=self._chat, reel_id=reel_id,
                                   read_error=True)

            if not self.nav.open_comments():
                self.nav.back_to_chat()
                return ReelContext(chat_name=self._chat, reel_id=reel_id,
                                   read_error=True)

            comments, count = self.collector.collect(
                limit=self.config.settings.comments_to_read)
            # comments sheet -> reel viewer -> thread
            self.nav.back_to_chat()

            return ReelContext(
                chat_name=self._chat, reel_id=reel_id,
                has_preceding_text=False, preceding_text=None,
                comments=comments, comment_count=count,
            )
        except Exception:
            log.exception("_read_reel failed for %s", reel_id)
            self.nav.back_to_chat()
            return ReelContext(chat_name=self._chat, reel_id=reel_id,
                               read_error=True)

    def _has_outgoing_reply_after(self, node: UiNode) -> bool:
        """True if an outgoing (right-aligned) message sits just below this reel
        — i.e. you've already replied to it. Best-effort revisit guard."""
        w, _ = self.d.window_size()
        reel_bottom = node.bounds[3]
        for tv in self.d.find_all(className="android.widget.TextView"):
            txt = (tv.text or "").strip()
            if not txt:
                continue
            l, t, r, b = tv.bounds
            cx = (l + r) // 2
            outgoing = cx > w / 2          # your messages are right-aligned
            gap = t - reel_bottom
            if outgoing and 0 <= gap < 240:
                return True
        return False

    def _relocate_reel_node(self, node: UiNode) -> UiNode | None:
        """Find the on-screen reel bubble closest to `node` (fresh bounds after
        navigation). Returns None if no reel bubble is currently visible."""
        cx0, cy0 = node.center
        best, best_d = None, 10 ** 18
        for n in self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view"):
            cx, cy = n.center
            d = (cx - cx0) ** 2 + (cy - cy0) ** 2
            if d < best_d:
                best, best_d = n, d
        return best

    def _preceding_text_for_node(self, node: UiNode) -> str | None:
        """Return the incoming text message immediately above the reel, if any.

        Rule 1: text right before a reel may carry context/an inside joke, so
        we must NOT automate. We look for an incoming (left-aligned) text bubble
        whose bottom edge sits just above the reel's top edge.
        """
        width, _ = self.d.window_size()
        reel_top = node.bounds[1]
        best_text = None
        best_bottom = -1
        for tv in self.d.find_all(className="android.widget.TextView"):
            txt = (tv.text or "").strip()
            if not txt:
                continue
            l, t, r, b = tv.bounds
            cx = (l + r) // 2
            incoming = cx < width / 2
            gap = reel_top - b
            if incoming and 0 <= gap < 220 and b > best_bottom:
                best_bottom = b
                best_text = txt
        return best_text
