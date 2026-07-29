"""The Backend interface every driver implements.

Keeping this seam tiny is what makes the modules independently replaceable, as
the spec requires. A backend is responsible for *navigation + observation only*
— it never decides anything. It hands back a `ReelContext`; the engine decides.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

from ..models import ReelContext


@dataclass
class ReelHandle:
    """An opaque pointer to one received reel inside the current chat."""
    reel_id: str
    # backend-specific locator data (element bounds, index, fixture key, ...)
    locator: object = None


class Backend(ABC):
    """Drives Instagram and observes state. Decides nothing."""

    @abstractmethod
    def prepare(self) -> None:
        """Ensure the app is open and we are at a known state (the inbox)."""

    @abstractmethod
    def open_chat(self, chat_name: str) -> bool:
        """Navigate into the named conversation. Return False if not found."""

    @abstractmethod
    def find_unreacted_reels(self) -> list[ReelHandle]:
        """Return received reels in the current chat that still need a reaction.

        Must exclude reels you sent, and reels you have already reacted/replied
        to, so re-running is idempotent and nobody gets double-tapped.
        """

    @abstractmethod
    def build_reel_context(self, reel: ReelHandle) -> ReelContext:
        """Open the reel, detect preceding text (Rule 1), collect comments,
        and return to the chat. All observation, no judgement."""

    @abstractmethod
    def send_reply(self, reel: ReelHandle, text: str) -> bool:
        """Send `text` as a reply to the reel in the current chat.

        For real backends the reel yielded by `iter_reels` is left open when
        this is called, so the reply can be attached to that specific reel.
        """

    @abstractmethod
    def return_to_inbox(self) -> None:
        """Get back to the inbox so the next chat can be processed."""

    # ---- plain-text messages --------------------------------------------
    def unanswered_incoming_texts(self, cap: int = 8) -> list[str]:
        """Return incoming plain-text messages in the current chat that arrived
        since our last reply (newest-first-bounded), most you'd want a human to
        answer. The bot only reacts to reels, so these are surfaced/flagged.

        Default: none (fixtures carry only reels). Real backends override.
        """
        return []

    # ---- per-reel iteration ---------------------------------------------
    def iter_reels(self) -> Iterator[tuple[ReelHandle, ReelContext]]:
        """Yield (reel, context) for each unreacted reel, one at a time.

        The caller must invoke exactly one of `send_reply(reel, ...)` or
        `discard_reel(reel)` for each yielded item before requesting the next.

        Default implementation enumerates everything up front, which is fine
        for fixtures. Real backends override this to interleave scrolling and
        observation so that reels which are off-screen (older in the thread)
        are still reachable, and so a reply can be sent while its reel is still
        open on screen.
        """
        for reel in self.find_unreacted_reels():
            yield reel, self.build_reel_context(reel)

    def discard_reel(self, reel: ReelHandle) -> None:
        """Called when a yielded reel will NOT be replied to (it was flagged,
        or the run is plan-only). Lets a real backend close whatever UI it
        opened to read the reel and return to a known state."""
        return None

    def close(self) -> None:  # optional cleanup hook
        pass
