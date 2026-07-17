"""Instagram Reel Auto-Reactor.

An on-demand assistant that reacts to reels sent to you in DMs — but only for
chats you explicitly enable, only for the obvious/high-confidence cases, and
never by staying online monitoring. Everything ambiguous is queued for you.

The reaction "brain" is fully deterministic (no LLMs) and has zero third-party
dependencies, so it runs and is testable anywhere Python runs.
"""

__version__ = "0.1.0"
