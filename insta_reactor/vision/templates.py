"""Template-matching fallback: 'find this icon on the screen'.

The spec's vision idea in its simplest robust form: keep small PNG crops of key
icons (comment bubble, send arrow, ...) under templates/, and locate them on a
screenshot via normalized cross-correlation. This survives IG moving an icon a
few pixels or restyling ids, because it matches on *appearance*.

opencv/numpy are imported lazily so nothing here is required unless you
actually call it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TemplateMatch:
    center: tuple[int, int]
    score: float
    bounds: tuple[int, int, int, int]


def find_template(screenshot, template_path: str,
                  threshold: float = 0.82) -> TemplateMatch | None:
    """Locate `template_path` inside `screenshot` (a PIL image or ndarray).

    Returns the best match above `threshold`, else None.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:  # pragma: no cover - env dependent
        raise RuntimeError(
            "Vision fallback needs opencv-python + numpy. "
            "Install with: pip install opencv-python numpy"
        ) from e

    # Accept PIL image or ndarray.
    if hasattr(screenshot, "convert"):
        scene = np.array(screenshot.convert("RGB"))[:, :, ::-1].copy()  # RGB->BGR
    else:
        scene = np.asarray(screenshot)

    template = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if template is None:
        raise FileNotFoundError(template_path)

    result = cv2.matchTemplate(scene, template, cv2.TM_CCOEFF_NORMED)
    _min_v, max_v, _min_l, max_l = cv2.minMaxLoc(result)
    if max_v < threshold:
        return None

    th, tw = template.shape[:2]
    left, top = max_l
    return TemplateMatch(
        center=(left + tw // 2, top + th // 2),
        score=float(max_v),
        bounds=(left, top, left + tw, top + th),
    )
