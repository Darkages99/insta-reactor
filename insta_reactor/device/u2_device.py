"""uiautomator2 implementation of the Device interface.

uiautomator2 (`pip install uiautomator2`) is the most robust Python option for
driving a real Android phone or emulator: it installs a small agent on the
device and exposes selector-based finds, taps, swipes, text input, and
screenshots. We wrap it behind our own `Device` so the rest of the app never
imports it directly.

This module is imported lazily (only when you actually run against a device),
so the deterministic core never needs uiautomator2 installed.
"""

from __future__ import annotations

import re

from .base import Device, UiNode


def _parse_bounds(s: str) -> tuple[int, int, int, int]:
    # "[0,100][400,200]" -> (0,100,400,200)
    m = re.findall(r"-?\d+", s or "")
    if len(m) == 4:
        return tuple(int(x) for x in m)  # type: ignore[return-value]
    return (0, 0, 0, 0)


class U2Device(Device):
    def __init__(self, serial: str = ""):
        try:
            import uiautomator2 as u2
        except ImportError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                "uiautomator2 is required for real-device runs. "
                "Install it with: pip install uiautomator2"
            ) from e
        self._u2 = u2
        self.d = u2.connect(serial) if serial else u2.connect()
        # Fail fast if the agent isn't reachable.
        self.d.info  # noqa: B018

    # ---- observation ----------------------------------------------------
    def current_package(self) -> str:
        try:
            return self.d.app_current().get("package", "")
        except Exception:
            return ""

    def screenshot(self, path: str | None = None):
        img = self.d.screenshot()  # PIL.Image
        if path:
            img.save(path)
        return img

    def dump_hierarchy(self) -> str:
        return self.d.dump_hierarchy()

    def _to_node(self, sel) -> UiNode | None:
        # Reads can race with UI animations: a node that `exists` one instant
        # can be recycled the next, making uiautomator2 raise
        # StaleObjectException (surfaced as RPCUnknownError). Treat any such
        # transient read failure as "not found" rather than crashing the run.
        try:
            if not sel.exists:
                return None
            info = sel.info
        except Exception:
            return None
        return UiNode(
            text=info.get("text") or "",
            desc=info.get("contentDescription") or "",
            resource_id=info.get("resourceName") or "",
            clazz=info.get("className") or "",
            bounds=self._bounds_from_info(info),
            clickable=bool(info.get("clickable")),
        )

    @staticmethod
    def _bounds_from_info(info: dict) -> tuple[int, int, int, int]:
        b = info.get("bounds") or {}
        return (b.get("left", 0), b.get("top", 0),
                b.get("right", 0), b.get("bottom", 0))

    def _selector(self, text=None, desc=None, resource_id=None,
                  textContains=None, descContains=None, className=None):
        kw = {}
        if text is not None:
            kw["text"] = text
        if desc is not None:
            kw["description"] = desc
        if resource_id is not None:
            kw["resourceId"] = resource_id
        if textContains is not None:
            kw["textContains"] = textContains
        if descContains is not None:
            kw["descriptionContains"] = descContains
        if className is not None:
            kw["className"] = className
        return self.d(**kw)

    def find(self, *, text=None, desc=None, resource_id=None,
             textContains=None, descContains=None, className=None) -> UiNode | None:
        return self._to_node(self._selector(
            text, desc, resource_id, textContains, descContains, className))

    def find_all(self, **kwargs) -> list[UiNode]:
        sel = self._selector(**kwargs)
        out: list[UiNode] = []
        try:
            count = sel.count
        except Exception:
            return out
        for i in range(count):
            try:
                node = self._to_node(sel[i])
            except Exception:
                # index shifted mid-iteration (list mutated); stop cleanly.
                break
            if node:
                out.append(node)
        return out

    # ---- actions --------------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self.d.click(x, y)

    def long_press(self, x: int, y: int, duration: float = 0.6) -> None:
        self.d.long_click(x, y, duration)

    def swipe(self, x1, y1, x2, y2, duration: float = 0.2) -> None:
        self.d.swipe(x1, y1, x2, y2, duration)

    def input_text(self, text: str) -> None:
        # Clear first: callers (search box, composer) always want the field
        # set to exactly `text`, and retries must not compound old input.
        self.d.send_keys(text, clear=True)

    def press_back(self) -> None:
        self.d.press("back")

    def press_home(self) -> None:
        self.d.press("home")

    def window_size(self) -> tuple[int, int]:
        return self.d.window_size()

    def start_app(self, package: str) -> None:
        self.d.app_start(package, use_monkey=True)
