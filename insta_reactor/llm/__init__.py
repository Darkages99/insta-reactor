"""Remote LLM support (OpenAI-compatible, e.g. OpenRouter).

Kept in its own package so the deterministic core never imports it unless the
LLM path is explicitly enabled. See client.py for the seam and factory.
"""
