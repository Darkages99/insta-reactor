"""Pluggable Instagram backends.

The runner depends only on the `Backend` interface, so the deterministic core
never knows or cares whether it is driving a real phone (`AndroidBackend`) or
reading a JSON fixture (`SimulatedBackend`). Swap freely.
"""

from .base import Backend, ReelHandle

__all__ = ["Backend", "ReelHandle"]
