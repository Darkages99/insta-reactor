"""Optional computer-vision fallback for locating controls by appearance.

Only used when accessibility selectors fail. Lazily imports opencv/numpy so the
core never depends on them.
"""

from .templates import find_template, TemplateMatch

__all__ = ["find_template", "TemplateMatch"]
