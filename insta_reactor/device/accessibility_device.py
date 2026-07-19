"""AccessibilityService implementation of the Device interface (V3, no PC).

This is the V3 replacement for `U2Device`. Where `U2Device` drove the phone via
uiautomator2 → ADB → a PC host, this drives it via Android's
**AccessibilityService** running *on the phone itself* — no ADB, no PC. See
`docs/V3_PLAN.md` §3.

The heavy lifting (reading `rootInActiveWindow`, dispatching `dispatchGesture`,
`ACTION_SET_TEXT`) lives in Kotlin (`ReactorAccessibilityService`). This class is
a thin Python wrapper over that service, reached through **Chaquopy's Java
interop** inside the V3 app. To keep the seam testable without an Android device,
the wrapper never imports Chaquopy or Android: it talks only to a small
duck-typed `bridge` object (see `AccessibilityBridge`). In the app the bridge is
the live Kotlin service instance; in unit tests it is a pure-Python fake.

Design contract — the bridge does *primitives only* (read the raw node list,
fire one gesture, set text). All selector logic, geometry, and `Device`-shaped
behaviour stays here in Python, so it ports 1:1 with the rest of the engine and
is covered by the same test suite.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from .base import Device, UiNode

log = logging.getLogger("insta_reactor.a11y")


@runtime_checkable
class AccessibilityBridge(Protocol):
    """The narrow surface the Kotlin `ReactorAccessibilityService` exposes to
    Python (via Chaquopy). Method names are camelCase because they are called
    verbatim on the Java object. Anything a real device needs that isn't a raw
    primitive is built *on top* of these, here in Python — not added here.

    The node contract (`nodesJson`) is a JSON array of flat node objects, one
    per node in `rootInActiveWindow`, in depth-first pre-order (so index 0 is
    the root and `find` returns the first match in tree order, matching u2):

        {
          "cls": "android.widget.TextView",   # getClassName()
          "text": "LMAOO",                     # getText()
          "desc": "",                          # getContentDescription()
          "id": "com.instagram.android:id/x",  # getViewIdResourceName()
          "bounds": [left, top, right, bottom],# getBoundsInScreen()
          "clickable": false,                  # isClickable()
          "scrollable": false,                 # isScrollable()   (extra, unused by UiNode)
          "longClickable": false,              # isLongClickable()(extra)
          "enabled": true                      # isEnabled()      (extra)
        }

    Missing/blank string fields are "" (never null). Extra keys are ignored by
    the parser, so the Kotlin side may add fields without breaking Python.
    """

    def currentPackage(self) -> str: ...
    def windowSize(self) -> Any: ...            # int[2] -> (width, height)
    def nodesJson(self) -> str: ...
    def tap(self, x: int, y: int) -> bool: ...
    def longPress(self, x: int, y: int, durationMs: int) -> bool: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, durationMs: int) -> bool: ...
    def inputText(self, text: str) -> bool: ...
    def pressBack(self) -> bool: ...
    def pressHome(self) -> bool: ...
    def startApp(self, package: str) -> bool: ...


class AccessibilityDevice(Device):
    """`Device` implemented over an on-device AccessibilityService bridge."""

    def __init__(self, bridge: AccessibilityBridge):
        if bridge is None:
            raise ValueError(
                "AccessibilityDevice needs a live service bridge. In the app "
                "this is ReactorAccessibilityService.instance (via Chaquopy); "
                "if it's None the accessibility service isn't enabled/connected."
            )
        self._b = bridge

    # ---- observation ----------------------------------------------------
    def current_package(self) -> str:
        try:
            return self._b.currentPackage() or ""
        except Exception:
            log.exception("currentPackage failed")
            return ""

    def window_size(self) -> tuple[int, int]:
        size = self._b.windowSize()
        # Java int[] (Chaquopy), a Python list/tuple — index either way.
        return int(size[0]), int(size[1])

    def screenshot(self, path: str | None = None):
        """Best-effort. The reel flow is driven entirely off the node tree, so a
        pixel screenshot isn't on the runtime path (only `tools/calibrate.py`
        uses it). AccessibilityService *can* grab one via `takeScreenshot`, but
        that's an async, API-30+ extra we only wire up if the bridge offers it.
        """
        getter = getattr(self._b, "screenshotPng", None)
        if getter is None:
            log.info("screenshot() unsupported by this bridge; returning None")
            return None
        try:
            data = bytes(getter())
        except Exception:
            log.exception("screenshotPng failed")
            return None
        if path:
            with open(path, "wb") as f:
                f.write(data)
        try:
            import io

            from PIL import Image  # optional; only if 'vision' extra installed
            return Image.open(io.BytesIO(data))
        except Exception:
            return data  # raw PNG bytes if PIL isn't available

    def dump_hierarchy(self) -> str:
        """u2 returned UIAutomator XML here; the a11y equivalent is our node
        JSON. Only the dev calibration tool consumes this, and it just writes it
        to a file for inspection, so the JSON serialisation is fine."""
        try:
            return self._b.nodesJson()
        except Exception:
            log.exception("nodesJson failed")
            return "[]"

    # ---- node reading & selection ---------------------------------------
    def _nodes(self) -> list[UiNode]:
        try:
            raw = self._b.nodesJson()
        except Exception:
            log.exception("nodesJson failed")
            return []
        try:
            data = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            log.exception("nodesJson returned unparseable payload")
            return []
        out: list[UiNode] = []
        for n in data:
            b = n.get("bounds") or [0, 0, 0, 0]
            if len(b) != 4:
                b = [0, 0, 0, 0]
            out.append(UiNode(
                text=n.get("text") or "",
                desc=n.get("desc") or "",
                resource_id=n.get("id") or "",
                clazz=n.get("cls") or "",
                bounds=(int(b[0]), int(b[1]), int(b[2]), int(b[3])),
                clickable=bool(n.get("clickable")),
            ))
        return out

    @staticmethod
    def _matches(node: UiNode, *, text, desc, resource_id,
                 textContains, descContains, className) -> bool:
        """AND of every supplied criterion, with the same semantics u2 uses:
        text/desc/resource_id/className are exact matches; the *Contains
        variants are substrings."""
        if text is not None and node.text != text:
            return False
        if desc is not None and node.desc != desc:
            return False
        if resource_id is not None and node.resource_id != resource_id:
            return False
        if className is not None and node.clazz != className:
            return False
        if textContains is not None and textContains not in node.text:
            return False
        if descContains is not None and descContains not in node.desc:
            return False
        return True

    def find(self, *, text: str | None = None, desc: str | None = None,
             resource_id: str | None = None, textContains: str | None = None,
             descContains: str | None = None,
             className: str | None = None) -> UiNode | None:
        for node in self._nodes():
            if self._matches(node, text=text, desc=desc, resource_id=resource_id,
                             textContains=textContains, descContains=descContains,
                             className=className):
                return node
        return None

    def find_all(self, **kwargs) -> list[UiNode]:
        crit = dict(text=None, desc=None, resource_id=None,
                    textContains=None, descContains=None, className=None)
        crit.update(kwargs)
        return [n for n in self._nodes() if self._matches(n, **crit)]

    # ---- actions --------------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self._b.tap(int(x), int(y))

    def long_press(self, x: int, y: int, duration: float = 0.6) -> None:
        self._b.longPress(int(x), int(y), int(duration * 1000))

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration: float = 0.2) -> None:
        self._b.swipe(int(x1), int(y1), int(x2), int(y2), int(duration * 1000))

    def input_text(self, text: str) -> None:
        # ACTION_SET_TEXT replaces the field's whole contents, matching
        # U2Device.input_text(clear=True): callers always want exactly `text`.
        self._b.inputText(text)

    def press_back(self) -> None:
        self._b.pressBack()

    def press_home(self) -> None:
        self._b.pressHome()

    def start_app(self, package: str) -> None:
        self._b.startApp(package)
