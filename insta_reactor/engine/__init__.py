"""Deterministic reaction engine (no LLMs, no third-party deps)."""

from .reaction import decide_reaction

__all__ = ["decide_reaction"]
