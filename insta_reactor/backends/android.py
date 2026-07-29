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
import re
from collections.abc import Iterator

from .base import Backend, ReelHandle
from ..config import AppConfig
from ..models import ReelContext, Comment
from ..device.factory import build_device
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

# Known IG UI hint strings that render as TextViews near a shared reel but are
# not chat messages at all (tap/hold hints, reaction hints, etc).
_UI_HINT_STRINGS = {
    "tap and hold to react",
    "double tap to react",
    "tap to react",
}

_HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._]{0,28}[a-z0-9])?$")

# TextView resource-ids that render inside or beside a shared-reel bubble but are
# NOT chat messages: the reel's own author-attribution label and the "tap to
# react" footer hint. Confirmed against a real DM-thread capture
# (data/calib_chat, P2): these carry stable IG-internal ids that a free-text DM
# bubble does not, so filtering by id is a far more robust discriminator than the
# text heuristic below (a bare lowercase handle like "thestevenhe" is otherwise
# indistinguishable from a one-word message). Used by the preceding/following/
# outgoing-reply scans so a reel author label never trips Rule 1/1b.
_NON_MESSAGE_TEXT_IDS = frozenset({
    "com.instagram.android:id/title_text",
    "com.instagram.android:id/message_footer_label",
})


def _is_ui_chrome_or_label(txt: str) -> bool:
    """True if `txt` is Instagram UI chrome (a tap/hold hint) or an account
    attribution label (e.g. "modern.aphorism") rather than an actual DM sent
    by a person. Both render as plain TextViews right next to a shared reel
    bubble, same as a real message would, so geometry alone can't tell them
    apart — we need to look at the text itself.

    A real DM almost never looks like an IG handle: handles have no spaces
    and are built from lowercase letters/digits/dots/underscores, usually
    with a "." or "_" in them. Casual chat text ("lol", "same", "😭") either
    has spaces, mixed case, punctuation, or emoji that a handle can't have.
    """
    low = txt.strip().lower()
    if low in _UI_HINT_STRINGS:
        return True
    if " " in txt:
        return False
    if ("." in txt or "_" in txt) and _HANDLE_RE.match(low):
        return True
    return False


class AndroidBackend(Backend):
    def __init__(self, config: AppConfig, device=None):
        self.config = config
        # V3 (on-device) passes an AccessibilityDevice here; the PC path leaves
        # it None and build_device() lazily constructs U2Device. See
        # device/factory.py.
        self.d = device or build_device(config)
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
        # Run-start position watermark: process only the reels newer than our
        # most recent pre-existing outgoing message (the ones that arrived since
        # we last replied). This is what makes idempotency and re-send handling
        # both work — see _new_reel_budget. Capped by max_new_reels and _MAX_REELS.
        budget = self._new_reel_budget()
        limit = min(budget, max_new, _MAX_REELS)
        log.info("iter_reels: new-reel budget=%d (cap max_new=%d) => processing %d",
                 budget, max_new, limit)
        k = 0
        while k < limit:
            node = self._locate_nth_reel_from_bottom(k)
            if node is None:
                log.info("no new reel #%d from bottom — handled %d new reel(s)",
                         k, k)
                return
            # NOTE: we deliberately do NOT skip here based on an on-screen
            # "already replied" check. That check (a reply bubble just below the
            # reel) is fundamentally unreliable: our own replies are always the
            # NEWEST messages in the thread, so they render directly beneath the
            # newest *incoming* reel — even though that reply belongs to an older
            # reel we quoted. Proximity can't tell which reel a quote answers, so
            # the guard false-positived on exactly the newest, still-unanswered
            # reel and skipped it every run (confirmed live). Dedup is instead
            # the runner's job via the cross-run comment signature (SeenStore),
            # which is content-based and reliable. We just read every reel here.
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
        """React to the reel by re-opening it and replying from the reel viewer.

        We deliberately do NOT use thread swipe-to-reply here: that gesture is
        timing-sensitive and, when it fails to arm the "replying to" quote chip,
        the old code fell through to the plain thread composer and sent a
        *floating* message with no reel attached (confirmed on a live run — a
        bare 😂 landed unquoted). Instead we tap the reel bubble to open the
        reel viewer and type into its own reply bar ("Reply to <name>"), which
        Instagram always attaches to *this* reel. If we can't open the viewer or
        find its reply bar, we return False so the runner flags it — we never
        fall back to an unattached send.

        Opening a reel preserves the thread's scroll position, so the original
        `node` bounds stay valid; we still relocate to fresh on-screen bounds
        first in case navigation shifted things.
        """
        node = reel.locator if isinstance(reel.locator, UiNode) else None
        if node is None:
            return False
        if self.nav.detect_state() != State.CHAT and not self.nav.back_to_chat():
            return False
        target = self._relocate_reel_node(node) or node
        self.d.tap_node(target)
        if not self.nav.wait_for_state(State.REEL_VIEWER):
            # couldn't open the reel — get back to a known state and bail.
            self.nav.back_to_chat()
            return False
        ok = self.nav.reply_in_reel_viewer(text)
        # comments/viewer -> thread, whatever happened, so the sweep can go on.
        self.nav.back_to_chat()
        return ok

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
            log.info("_locate_nth_reel_from_bottom: _anchor_chat_bottom failed")
            return None
        w, h = self.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
        all_nodes = self.d.find_all()
        all_reels = [n for n in all_nodes
                     if n.resource_id == "com.instagram.android:id/reel_share_item_view"]
        ids_seen = sorted({n.resource_id for n in all_nodes if n.resource_id})
        log.info("_locate_nth_reel_from_bottom: window=%dx%d zone=(%d,%d) "
                  "total_nodes=%d reel nodes=%d %s",
                  w, h, zt, zb, len(all_nodes), len(all_reels),
                  [(n.bounds, n.center) for n in all_reels])
        log.info("resource-ids seen (%d): %s", len(ids_seen), ids_seen)

        skipped = 0
        for step in range(_MAX_SWEEP_STEPS):
            raw = self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view")
            reels = self._loosely_visible_reels(zt, zb)
            log.info("sweep step %d: raw_reel_nodes=%d %s loosely_visible=%d skipped=%d",
                      step, len(raw), [(n.bounds, n.center) for n in raw],
                      len(reels), skipped)
            if reels:
                newest = reels[-1]          # largest y => closest to bottom
                if skipped == k:
                    return newest
                # advance the cursor: push this reel's CENTER below the fold so
                # the next scan's bottom-most reel is the preceding (older) one.
                # (Center, not bottom — the loose-visibility test keys off the
                # center; pushing only the bottom re-selected the same reel.)
                self.nav.scroll_node_below_fold(newest.center[1], zb)
                skipped += 1
                continue
            # nothing visible in the band — reveal older messages above.
            # A half-screen stride (the thread_scroll_up default) is wider than
            # the zone-minus-tallest-reel margin, so a reel bubble can be
            # scrolled clean from "not yet rendered" to "already past" between
            # two consecutive checks without ever being attached/queryable in
            # between (RecyclerView only attaches items landing in the final
            # post-scroll range). A narrower stride keeps consecutive zone
            # checks overlapping enough that no reel-sized bubble slips through.
            progressed = self.nav.thread_scroll_up(amount=0.3)
            log.info("sweep step %d: nothing visible, thread_scroll_up()=%s", step, progressed)
            if not progressed:
                # at the very top: count any remaining fully-visible reels,
                # newest (bottom-most) first.
                fv = self._fully_visible_reels(zt, zb)
                log.info("sweep step %d: reached top, fully_visible=%d", step, len(fv))
                for n in reversed(fv):
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

            following = self._following_text_for_node(node)
            if following:
                # Rule 1b: sender's own follow-up right after the reel —
                # usually an inside joke needing a human reply.
                return ReelContext(
                    chat_name=self._chat, reel_id=reel_id,
                    has_following_text=True, following_text=following,
                    comments=[], comment_count=0,
                )

            self.d.tap_node(node)
            if not self.nav.wait_for_state(State.REEL_VIEWER):
                self.nav.back_to_chat()
                return ReelContext(chat_name=self._chat, reel_id=reel_id,
                                   read_error=True)

            if not self.nav.open_comments():
                learned = (self.nav.comments_button_center is None
                           and self._learn_comments_button_coords(node))
                if not (learned and self.nav.open_comments()):
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

    def _attribution_labels(self) -> set[str]:
        """Text of every reel-attribution label currently on screen, queried
        directly by resource-id (not via the generic TextView sweep).

        Belt-and-suspenders for the `_NON_MESSAGE_TEXT_IDS` filter below:
        confirmed on-device that a plain TextView scan can occasionally read
        an attribution label's `resourceName` back empty (a transient
        uiautomator2/accessibility read, not a real absence of the id) —
        which let a reel author's handle (e.g. "yasirmemebaaz") slip through
        as if it were a real chat message and wrongly trip Rule 1/1b. Querying
        by resource-id directly is a separate lookup less prone to that same
        flake, so cross-checking text against this set catches it either way.
        """
        return {
            n.text.strip()
            for n in self.d.find_all(
                resource_id="com.instagram.android:id/title_text")
            if n.text and n.text.strip()
        }

    # NOTE: the former on-screen "already replied" guard (_has_outgoing_reply_after)
    # was removed. It looked for an outgoing reply bubble just below a *specific*
    # reel and used PROXIMITY to decide that reel was answered — unreliable,
    # because our replies are always the newest messages, so they sit directly
    # beneath the newest *incoming* reel while actually belonging to an older reel
    # we quoted. Proximity can't attribute a quote to a specific reel.
    #
    # The watermark below is different and robust: it does NOT attribute a reply
    # to a reel. It only finds the single thread-ORDER boundary — our most recent
    # outgoing message — and treats reels newer than it as unreacted. That answers
    # "have we replied since this reel arrived?" purely by order, which survives
    # re-sends (a re-shared reel is a NEW bubble newer than our last reply, so it
    # gets reacted even though we reacted to an identical reel before). See the
    # resend-reels-should-react requirement.

    def _newest_outgoing_top(self, zt: int, zb: int, w: int) -> int | None:
        """Top-y of the BOTTOM-MOST outgoing (right-aligned) message currently in
        the tappable band, or None if none is visible. Outgoing = our own
        messages: right-aligned reel-quote bubbles (our reactions) and
        right-aligned text/emoji. This is the run-start watermark's anchor; it is
        used only to locate the thread-order boundary between reels we've already
        replied to and newer ones — never to attribute a reply to a specific reel.
        """
        tops: list[int] = []
        for rn in self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view"):
            l, t, r, b = rn.bounds
            cx = (l + r) // 2
            if cx > w / 2 and t >= zt and b <= zb:   # right-aligned => outgoing
                tops.append(t)
        attribution = self._attribution_labels()
        for tv in self.d.find_all(className="android.widget.TextView"):
            if tv.resource_id in _NON_MESSAGE_TEXT_IDS:
                continue  # reel author label / react-hint footer, not a message
            txt = (tv.text or "").strip()
            if not txt or txt in attribution or _is_ui_chrome_or_label(txt):
                continue
            l, t, r, b = tv.bounds
            cx = (l + r) // 2
            if cx > w / 2 and t >= zt and b <= zb:   # right-aligned => outgoing
                tops.append(t)
        return max(tops) if tops else None

    def _new_reel_budget(self) -> int:
        """How many reels at the bottom of the thread are NEWER than our most
        recent PRE-EXISTING outgoing message — i.e. reels that arrived since we
        last replied and so still need a reaction.

        Computed once at run start, BEFORE we post any reply, so our own new
        replies never move the watermark (which would strand older-but-still-new
        reels: after reacting to the newest reel, our reply becomes the newest
        message, making every remaining unreacted reel look "already replied to").

        A re-sent reel counts, because its new bubble is newer than our last reply
        even if we reacted to an identical reel before (resend-reels-should-react).
        Idempotency holds too: on a re-run with no new reels, our last reply is
        the newest message and nothing is below it, so the count is 0.

        If we have never replied in this thread there is no watermark, so every
        incoming reel is new. The count is capped by max_new_reels.
        """
        if not self._anchor_chat_bottom():
            return 0
        w, h = self.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
        cap = min(getattr(self.config.settings, "max_new_reels", 3), _MAX_REELS)
        count = 0
        for step in range(_MAX_SWEEP_STEPS):
            reels = self._loosely_visible_reels(zt, zb)     # incoming, y-asc
            wm_top = self._newest_outgoing_top(zt, zb, w)   # bottom-most outgoing
            if not reels:
                if wm_top is not None:
                    log.info("_new_reel_budget: watermark reached, no reels "
                             "below it; count=%d", count)
                    return min(count, cap)
                if not self.nav.thread_scroll_up(amount=0.3):
                    log.info("_new_reel_budget: reached thread top; count=%d",
                             count)
                    return min(count, cap)
                continue
            bottom_reel = reels[-1]         # largest y => closest to bottom
            if wm_top is not None and wm_top > bottom_reel.bounds[1]:
                # our most recent outgoing message sits BELOW the bottom-most
                # visible reel => that reel (and everything older above it)
                # predates our last reply and is already handled. Stop.
                log.info("_new_reel_budget: watermark(top=%d) is below bottom "
                         "reel(top=%d) => stop; count=%d",
                         wm_top, bottom_reel.bounds[1], count)
                return min(count, cap)
            count += 1
            log.info("_new_reel_budget: counted new reel #%d (top=%d, wm_top=%s)",
                     count - 1, bottom_reel.bounds[1], wm_top)
            if count >= cap:
                return cap
            # advance past this reel by pushing its CENTER below the fold (see
            # scroll_node_below_fold) so we don't re-count the same one.
            self.nav.scroll_node_below_fold(bottom_reel.center[1], zb)
        return min(count, cap)

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

    def _learn_comments_button_coords(self, problem_node: UiNode) -> bool:
        """Recover from a reel whose comments button the selector can't find
        (e.g. a white icon on a white-background reel — confirmed live on
        shyam's "artby_arco" reel) by sampling the button's screen position
        from a DIFFERENT reel in the same thread, then returning to the one
        we actually want to react to. The reel-viewer bottom bar sits at a
        fixed screen position for every reel regardless of its content, so a
        coordinate learned from one reel is reusable on another. Leaves us
        back in `problem_node`'s reel viewer on success; caller must retry
        open_comments() (now backed by the learned coordinate) afterwards.
        """
        if self.nav.detect_state() != State.CHAT and not self.nav.back_to_chat():
            log.info("_learn_comments_button_coords: couldn't get back to CHAT")
            return False
        donor = None
        for n in self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view"):
            if n.bounds != problem_node.bounds:
                donor = n
                break
        if donor is None:
            # no other reel currently on screen — scroll to reveal a neighbour.
            self.nav.thread_scroll_up(amount=0.3)
            for n in self.d.find_all(
                    resource_id="com.instagram.android:id/reel_share_item_view"):
                if n.bounds != problem_node.bounds:
                    donor = n
                    break
        if donor is None:
            log.info("_learn_comments_button_coords: no donor reel available")
            return False
        log.info("_learn_comments_button_coords: donor reel bounds=%s", donor.bounds)

        self.d.tap_node(donor)
        if self.nav.wait_for_state(State.REEL_VIEWER):
            btn = self.nav._find_any(S.OPEN_COMMENTS_BUTTON)
            if btn is not None:
                self.nav.comments_button_center = btn.center
                log.info("_learn_comments_button_coords: learned %s from donor "
                          "reel", btn.center)
            else:
                log.info("_learn_comments_button_coords: donor reel opened but "
                          "its comments button wasn't found either")
        else:
            log.info("_learn_comments_button_coords: donor reel didn't open "
                      "(never reached REEL_VIEWER)")
        self.nav.back_to_chat()
        if self.nav.comments_button_center is None:
            return False

        target = self._relocate_reel_node(problem_node) or problem_node
        self.d.tap_node(target)
        reopened = self.nav.wait_for_state(State.REEL_VIEWER)
        log.info("_learn_comments_button_coords: reopened problem reel=%s",
                  reopened)
        return reopened

    def _preceding_text_for_node(self, node: UiNode) -> str | None:
        """Return the incoming text message immediately above the reel, if any.

        Rule 1: text right before a reel may carry context/an inside joke, so
        we must NOT automate. We look for an incoming (left-aligned) text bubble
        whose bottom edge sits just above the reel's top edge.

        A text bubble sandwiched between two reels is the sender's follow-up
        comment on the OLDER reel above it, not a preamble to the newer one
        below — confirmed live (shyam chat: "Nakiduchchu po" sat between an
        older reel and the newest one, and got wrongly attributed as preceding
        text of the newest reel, permanently blocking it from ever
        auto-replying). So we skip any candidate that already sits within the
        following-text gap of a different, older reel.
        """
        width, _ = self.d.window_size()
        reel_top = node.bounds[1]
        older_reel_bottoms = [
            n.bounds[3] for n in self.d.find_all(
                resource_id="com.instagram.android:id/reel_share_item_view")
            if n.bounds[3] <= reel_top and n.bounds != node.bounds
        ]
        attribution = self._attribution_labels()
        best_text = None
        best_bottom = -1
        for tv in self.d.find_all(className="android.widget.TextView"):
            if tv.resource_id in _NON_MESSAGE_TEXT_IDS:
                continue  # reel author label / react-hint footer, not a message
            txt = (tv.text or "").strip()
            if not txt or txt in attribution or _is_ui_chrome_or_label(txt):
                continue
            l, t, r, b = tv.bounds
            cx = (l + r) // 2
            incoming = cx < width / 2
            gap = reel_top - b
            if not (incoming and 0 <= gap < 220 and b > best_bottom):
                continue
            claimed_by_older_reel = any(
                0 <= (t - ob) < 220 for ob in older_reel_bottoms)
            if claimed_by_older_reel:
                continue
            best_bottom = b
            best_text = txt
        return best_text

    def _following_text_for_node(self, node: UiNode) -> str | None:
        """Return the incoming text message immediately after the reel, if any.

        Rule 1b: the sender's own follow-up right after sharing a reel usually
        carries an inside joke or specific comment, so we must NOT automate.
        We look for an incoming (left-aligned) text bubble whose top edge sits
        just below the reel's bottom edge. Must be called before we've sent
        any reply of our own, so any incoming bubble found here predates and
        is unrelated to our own (outgoing, right-aligned) reply.
        """
        width, _ = self.d.window_size()
        reel_bottom = node.bounds[3]
        attribution = self._attribution_labels()
        best_text = None
        best_top = 10 ** 9
        for tv in self.d.find_all(className="android.widget.TextView"):
            if tv.resource_id in _NON_MESSAGE_TEXT_IDS:
                continue  # reel author label / react-hint footer, not a message
            txt = (tv.text or "").strip()
            if not txt or txt in attribution or _is_ui_chrome_or_label(txt):
                continue
            l, t, r, b = tv.bounds
            cx = (l + r) // 2
            incoming = cx < width / 2
            gap = t - reel_bottom
            if incoming and 0 <= gap < 220 and t < best_top:
                best_top = t
                best_text = txt
        return best_text
