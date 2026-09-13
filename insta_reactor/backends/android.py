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
# Cold-start's scan_organic_reactions walks the WHOLE thread history in one
# continuous O(n) pass (not the live hot-path's bounded "skip ~3 reels" case
# that _MAX_SWEEP_STEPS=60 is sized for), so it needs a much bigger step
# budget or it silently truncates history scans partway up the thread (looks
# like "stops scrolling"), biasing the derived profile toward only the most
# recent handful of reactions. Confirmed live: since the walk is now linear
# rather than the old O(n^2) re-anchor-per-index sweep, a much larger budget
# here no longer means a quadratic blowup in run time — it was raised from
# 400 (sized for the old O(n^2) cost) accordingly. This is an offline/manual
# tool, not latency-sensitive, so give it a big budget instead of touching
# the live-run constant.
_COLDSTART_MAX_SWEEP_STEPS = 1500

# Incoming-text detection tuning. A shared reel's own caption/description renders
# as a plain TextView near the reel and can be hundreds of chars long; a typed DM
# almost never is. Anything longer than this is treated as a reel caption / pasted
# blurb, not a chat message worth a "reply manually" alert.
_MAX_TEXT_MSG_LEN = 220
# How far up the thread to hunt for new text when we CAN see our last reply
# (bounded by that watermark anyway). Without a watermark we don't scroll at all.
_MAX_TEXT_SCROLL_STEPS = 8
# Snippet length for the flag reason / push body (full text lives in the app).
_TEXT_SNIPPET_LEN = 160

# Known IG UI hint strings that render as TextViews near a shared reel but are
# not chat messages at all (tap/hold hints, reaction hints, etc).
_UI_HINT_STRINGS = {
    "tap and hold to react",
    "double tap to react",
    "tap to react",
}

# Cold-start history scan (scan_organic_reactions): a quoted-reply's embedded
# thumbnail re-renders the SAME reel_share_item_view widget at roughly half the
# width of a genuine shared reel (confirmed on-device: ~239px quote-preview vs
# ~478px real share, on a 1272px-wide screen). Width, not side-of-screen, is
# what tells them apart — a quote-preview is always right-aligned regardless of
# which reel it quotes.
_QUOTE_REEL_MAX_WIDTH = 320
# Thread timestamp dividers grow a longer date prefix the older the message
# is ("10:30" -> "THU, 6:18 PM" -> "YESTERDAY 10:25 PM" -> "6 JUL, 9:56 PM" ->
# presumably "6 JUL 2024, 9:56 PM" for messages over a year old), and these
# TextViews carry no resource-id to filter on (confirmed live — id is always
# ""), so enumerating every date-prefix format by regex is a losing game
# (confirmed live: "6 JUL, 9:56 PM" slipped through the weekday/YESTERDAY-only
# version and got scanned as a reply). Instead use a structural rule: a
# divider is text ending in a time expression where everything before that
# time is only uppercase letters/digits/commas/spaces — a real typed message
# essentially never looks like that, while every IG divider format does.
_TIME_SUFFIX_RE = re.compile(r"\d{1,2}:\d{2}(?:\s?[AP]M)?$")
_TIMESTAMP_PREFIX_RE = re.compile(r"^[A-Z0-9,\s]*$")


def _is_timestamp_divider(txt: str) -> bool:
    m = _TIME_SUFFIX_RE.search(txt)
    if not m:
        return False
    return bool(_TIMESTAMP_PREFIX_RE.match(txt[:m.start()]))


_SLEEP_MODE_MARKERS = ("sleep mode", "consider closing instagram")

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

    def iter_own_recent_messages(self, cap: int = 200) -> list[str]:
        """Cold-start hook: your own replies to reels in the currently open
        chat, newest-first. Backed by `scan_organic_reactions` (read-only —
        scrolls and reads, never taps Send)."""
        return [r["reply_text"] for r in self.scan_organic_reactions(max_reels=cap)]

    def scan_organic_reactions(self, max_reels: int = 40,
                                resume_after: dict | None = None,
                                out_state: dict | None = None,
                                on_group=None) -> list[dict]:
        """Walk the open thread from the newest message upward, pairing each
        received/quoted reel with YOUR reply immediately below it.

        Two on-device reaction shapes both fall out of the same rule (a reel
        bubble immediately followed by your own outgoing text/emoji, before
        the next reel):
          * a quick reaction — a short outgoing emoji directly under a
            full-width, left-aligned (incoming) reel share, no quote UI.
          * a full quoted reply — "You replied" + a half-width quote-preview
            thumbnail (same reel_share_item_view widget, ~half the width of a
            real share — see _QUOTE_REEL_MAX_WIDTH) + your text/emoji below it.

        A single continuous walk (anchor at the bottom once, then repeatedly
        emit the current bottom-most in-zone group and scroll it below the
        fold) — O(n) scroll calls for n groups, not the O(n^2) re-anchor-per-
        index sweep this used to be (see git history / memory for why that
        existed: a big scroll_node_below_fold distance for a reel sitting well
        above zone_bottom used to under-register on a single long swipe and
        re-land on the same screen, silently duplicating one reply many times
        over). That root cause is now fixed at the source — see
        `Navigator.scroll_node_below_fold`, which splits into capped,
        signature-verified chunks instead of trusting one long swipe — so the
        continuous walk this docstring used to warn against is safe again.
        As defense in depth, a stall guard (`last_bounds`) still bails out if
        the "newest" in-zone group's bounds are ever bit-for-bit identical to
        the previous step's, rather than looping forever.
        Read-only: only scrolls and reads text; never taps a reel open or
        sends anything.
        Returns newest-first `{"reply_text": ..., "kind": "full"|"quote"}`.

        `resume_after`, if given, is the {"kind", "reply_text"} of the oldest
        group a PREVIOUS call reached (see onboarding/coldstart_state.py).
        Groups are walked and skipped (not counted toward max_reels, not
        returned) until that exact one is seen again, then only groups OLDER
        than it are collected — so re-running on the same chat resumes
        instead of re-emitting duplicates. There's no stable reel id to match
        on across process runs, so this is a best-effort content+order
        anchor; harmless if it's ever a little off, since unanswered
        (reply_text=None) groups never get collected either way.

        `out_state`, if given, is filled in-place with `next_resume_after`
        (the oldest group actually reached this call) and `reached_watermark`.
        Kept for callers that only care about the final tally.

        `on_group`, if given, is called as `on_group(found, is_new)` right
        after EVERY group is located — before this scan has any chance to be
        interrupted. `is_new` is True once we're past the old resume marker
        (i.e. this group would count toward the returned list). This is what
        makes the scan resumable mid-run and not just at a clean exit: a
        caller can persist the resume marker (and any new pair) after each
        single group instead of waiting for the whole call to return, so a
        kill/interrupt loses at most the one group in flight, never the
        whole run. Cold-start scans on a live phone happen in short bursts,
        not one sitting, so this matters more here than elsewhere in the
        file."""
        out: list[dict] = []
        state = {"watermark_hit": resume_after is None, "last_seen": None}

        def _process(newest: UiNode, reels: list[UiNode]) -> bool:
            """Emit `newest`'s group; return True once max_reels is reached."""
            found = self._emit_reel_group(newest, reels)
            state["last_seen"] = found
            is_new = state["watermark_hit"]
            if not state["watermark_hit"] and \
                    (found.get("kind"), found.get("reply_text")) == \
                    (resume_after.get("kind"), resume_after.get("reply_text")):
                state["watermark_hit"] = True
            if on_group is not None:
                on_group(found, is_new)
            if is_new and found["reply_text"]:
                out.append(found)
            return len(out) >= max_reels

        if self._anchor_chat_bottom():
            w, h = self.d.window_size()
            zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
            last_bounds: tuple | None = None
            for _ in range(_COLDSTART_MAX_SWEEP_STEPS):
                reels = self._all_reel_nodes_in_zone(zt, zb)
                if reels:
                    newest = reels[-1]        # largest y => closest to bottom
                    if newest.bounds == last_bounds:
                        break   # stall guard: scroll made no visible progress
                    last_bounds = newest.bounds
                    if _process(newest, reels):
                        break
                    self.nav.scroll_node_below_fold(newest.center[1], zb)
                    continue
                if not self.nav.thread_scroll_up(amount=0.3):
                    # genuinely at the top: squeeze out any remaining groups
                    # still sitting in the zone instead of giving up.
                    reels = self._all_reel_nodes_in_zone(zt, zb)
                    for newest in reversed(reels):
                        if newest.bounds == last_bounds:
                            continue
                        last_bounds = newest.bounds
                        if _process(newest, reels):
                            break
                    break

        if out_state is not None:
            out_state["next_resume_after"] = state["last_seen"]
            out_state["reached_watermark"] = state["watermark_hit"]
        return out

    def _emit_reel_group(self, newest: UiNode, reels: list[UiNode]) -> dict:
        """Classify `newest` (the current bottom-most in-zone reel) and pair
        it with your reply below it, if any. Shared by `scan_organic_reactions`'s
        walk for both the in-zone and top-of-thread tail cases."""
        w, h = self.d.window_size()
        zt = int(h * _ZONE_TOP)
        width = newest.bounds[2] - newest.bounds[0]
        is_quote = width < _QUOTE_REEL_MAX_WIDTH
        incoming = newest.center[0] < w / 2
        if not is_quote and not incoming:
            # a full-width reel YOU sent — not a reaction target.
            return {"reply_text": None, "kind": "sent"}
        # Bottom bound for the reply-text search: the top of the next
        # newer reel if there is one (so we don't leak into that group's
        # reply), else the full window height. NOT zb — zb is a
        # tap-safety band for reels that must be safely tappable to
        # open, but this search only reads text, never taps it, and the
        # newest group's reply can legitimately sit just below zb, right
        # above the composer (confirmed live: a reaction emoji at
        # y=2287 on a 2772-tall screen with zb=2273 was being dropped).
        next_top = min((n.bounds[1] for n in reels
                        if n.bounds[1] > newest.bounds[3]),
                       default=h)
        reply = self._first_outgoing_text_below(newest, zt, next_top, w)
        return {"reply_text": reply, "kind": "quote" if is_quote else "full"}

    def _all_reel_nodes_in_zone(self, zt: int, zb: int) -> list[UiNode]:
        """Every reel_share_item_view (either side, any width) whose tap
        target is safely on-screen, top-first. Mirrors `_loosely_visible_reels`
        (bounds-top for the top edge, center for the bottom edge) rather than
        a plain center-in-range test — a center-only test lets a node whose
        top has already scrolled above zt keep registering as "in zone" after
        `scroll_node_below_fold`, so the sweep re-selects the same group on
        the next step instead of advancing (confirmed live: produced the same
        reply 6x in a row instead of walking to older groups)."""
        out = [n for n in self.d.find_all(
                    resource_id="com.instagram.android:id/reel_share_item_view")
               if n.bounds[1] >= zt and n.center[1] <= zb]
        return sorted(out, key=lambda n: n.bounds[1])

    def _first_outgoing_text_below(self, reel_node: UiNode, zt: int, zb: int,
                                    w: int) -> str | None:
        """The nearest outgoing (right-aligned) real message text below
        `reel_node`'s bottom edge and within the tappable band — your reaction
        to that reel, if any. Filters out the same non-message chrome the
        preceding/following-text scans do, plus the "You replied" label and
        thread timestamp dividers, which are unique to history scanning.

        Also rejects text sitting on a reposted tweet/post card: those cards
        reuse the SAME reel_share_item_view id as a genuine video Reel (no
        resource-id or content-description tells them apart — confirmed live,
        every node here has an empty id), and a card's own caption ("thephil
        clifton Ancient Greeks?") can land right of center and get mistaken
        for your reply. Visually, though, your real reply always sits on a
        solid, vividly-colored chat bubble; a card caption sits on the card's
        own dark/grey strip. Bubble fill color IS available (unlike id/desc),
        via a screenshot sampled at each candidate's bounds — see
        `_looks_like_reply_bubble`."""
        reel_bottom = reel_node.bounds[3]
        attribution = self._attribution_labels()
        screenshot = None
        best_text, best_top = None, 10 ** 9
        for tv in self.d.find_all(className="android.widget.TextView"):
            if tv.resource_id in _NON_MESSAGE_TEXT_IDS:
                continue
            txt = (tv.text or "").strip()
            if not txt or txt in attribution or _is_ui_chrome_or_label(txt):
                continue
            low = txt.lower()
            if low == "you replied" or _is_timestamp_divider(txt):
                continue
            if any(marker in low for marker in _SLEEP_MODE_MARKERS):
                continue
            if len(txt) > _MAX_TEXT_MSG_LEN:
                continue
            l, t, r, b = tv.bounds
            if t < reel_bottom or t > zb:
                continue
            cx = (l + r) // 2
            if cx < w / 2:
                continue          # incoming — not your reaction
            if t >= best_top:
                continue
            if screenshot is None:
                screenshot = self.d.screenshot()
            if not self._looks_like_reply_bubble(screenshot, tv.bounds):
                continue
            best_top, best_text = t, txt
        return best_text

    @staticmethod
    def _looks_like_reply_bubble(screenshot, bounds: tuple[int, int, int, int]) -> bool:
        """True if `bounds` sits on a vividly-colored chat-bubble fill rather
        than a dark/grey card strip. Samples a small grid inside the text
        bounds and takes the most saturated sample as a proxy for the
        bubble's fill (text glyphs and card chrome are both close to
        black/white/grey — low saturation — so the bubble fill, if present,
        stands out as the max). Threshold (green channel dominant) matches
        this app's outgoing-message bubble color, confirmed live via pixel
        sampling: real bubble ~(134, 213, 98) vs. card caption ~(30, 60, 80)."""
        l, t, r, b = bounds
        xs = range(l + 2, max(l + 3, r - 1), max(1, (r - l) // 6))
        ys = range(t + 2, max(t + 3, b - 1), max(1, (b - t) // 4))
        best_sample, best_sat = (0, 0, 0), -1
        for x in xs:
            for y in ys:
                if 0 <= x < screenshot.width and 0 <= y < screenshot.height:
                    px = screenshot.getpixel((x, y))[:3]
                    sat = max(px) - min(px)
                    if sat > best_sat:
                        best_sat, best_sample = sat, px
        r_, g_, b_ = best_sample
        return g_ > 150 and g_ - r_ > 30 and g_ - b_ > 30

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

    def send_reaction(self, reel: ReelHandle, emoji: str) -> bool:
        """Native long-press-style reaction via the reel viewer's reaction
        sheet. Same navigation shape as `send_reply`; only the final in-viewer
        action differs. Returns False (never raises) on any failure so the
        runner can fall back to `send_reply`."""
        node = reel.locator if isinstance(reel.locator, UiNode) else None
        if node is None:
            return False
        if self.nav.detect_state() != State.CHAT and not self.nav.back_to_chat():
            return False
        target = self._relocate_reel_node(node) or node
        self.d.tap_node(target)
        if not self.nav.wait_for_state(State.REEL_VIEWER):
            self.nav.back_to_chat()
            return False
        ok = self.nav.react_to_reel_in_viewer(emoji)
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

    def _reel_rects(self) -> list[tuple[int, int, int, int]]:
        """Bounds of every shared-reel bubble on screen. Used to drop a reel's
        own caption/description TextViews, which render INSIDE the bubble and
        would otherwise look like incoming chat text (confirmed live on freakhan:
        a long Hungarian reel caption leaked in as four 'messages')."""
        return [n.bounds for n in self.d.find_all(
            resource_id="com.instagram.android:id/reel_share_item_view")]

    def _incoming_text_bubbles(self, zt: int, zb: int, w: int) -> list[tuple[int, str]]:
        """(top_y, text) for every incoming (left-aligned) plain-text message
        bubble currently in the tappable band. Filters out the same non-message
        TextViews the reel-context scans do: reel author labels, react-hint
        footers, IG UI chrome, and bare handles — plus any TextView whose center
        sits inside a shared-reel bubble (that's the reel's own caption, not a
        chat message). Sorted top-first (y-asc)."""
        attribution = self._attribution_labels()
        reel_rects = self._reel_rects()
        out: list[tuple[int, str]] = []
        for tv in self.d.find_all(className="android.widget.TextView"):
            if tv.resource_id in _NON_MESSAGE_TEXT_IDS:
                continue
            txt = (tv.text or "").strip()
            if not txt or txt in attribution or _is_ui_chrome_or_label(txt):
                continue
            if len(txt) > _MAX_TEXT_MSG_LEN:
                # a reel caption / pasted blurb, not a typed message
                log.info("skipping long non-message text (%d chars): %.40r",
                         len(txt), txt)
                continue
            l, t, r, b = tv.bounds
            cx, cy = (l + r) // 2, (t + b) // 2
            if not (cx < w / 2 and t >= zt and b <= zb):   # left-aligned, in band
                continue
            if any(rl <= cx <= rr and rt <= cy <= rb
                   for (rl, rt, rr, rb) in reel_rects):     # reel's own caption
                continue
            out.append((t, txt))
        return sorted(out, key=lambda it: it[0])

    def unanswered_incoming_texts(self, cap: int = 8) -> list[str]:
        """Incoming plain-text messages newer than our most recent reply.

        Same run-start position watermark idea as `_new_reel_budget`: our own
        most recent outgoing message marks the boundary between what we've
        already dealt with and what arrived since. Any incoming text bubble
        BELOW that boundary (newer) is unanswered and gets surfaced — the bot
        only reacts to reels, so it can never answer text itself.

        Scrolling policy (deliberately conservative to avoid alert-flooding):
          * With a watermark visible, we may scroll up to gather multi-screen
            runs of new text, but the watermark bounds it and we cap the steps.
          * With NO watermark (we've never replied, or our last reply is off the
            bottom screen), we do NOT go spelunking through the whole history —
            we report only the current bottom screenful, since we can't tell
            what's genuinely new from what's ancient.
        Returned oldest-first, each truncated to a notification-friendly snippet.
        """
        if not self._anchor_chat_bottom():
            return []
        w, h = self.d.window_size()
        zt, zb = int(h * _ZONE_TOP), int(h * _ZONE_BOTTOM)
        collected: list[str] = []   # newest-first while building
        seen: set[str] = set()
        for _ in range(_MAX_TEXT_SCROLL_STEPS):
            wm_top = self._newest_outgoing_top(zt, zb, w)
            bubbles = self._incoming_text_bubbles(zt, zb, w)
            reached = False
            for t, txt in reversed(bubbles):        # newest (lowest) first
                if wm_top is not None and t <= wm_top:
                    reached = True                  # older than our last reply
                    break
                if txt in seen:
                    continue
                seen.add(txt)
                collected.append(txt)
                if len(collected) >= cap:
                    reached = True
                    break
            if reached or wm_top is None:
                # reached the watermark/cap, OR no watermark => don't history-dive
                break
            if not self.nav.thread_scroll_up(amount=0.3):
                break
        snip = [t if len(t) <= _TEXT_SNIPPET_LEN else t[:_TEXT_SNIPPET_LEN] + "…"
                for t in reversed(collected)]
        return snip

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
