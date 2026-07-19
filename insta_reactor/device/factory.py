"""One place that chooses which `Device` implementation to build.

This is the seam `docs/V3_PLAN.md` §7 (P1) calls for: "parameterise the device,
don't hardcode the import." Two concrete devices exist:

  * `U2Device`            — uiautomator2 → ADB → PC host          (V2, needs a PC)
  * `AccessibilityDevice` — on-device AccessibilityService bridge (V3, no PC)

Callers that already hold a device (e.g. the V3 app, which constructs
`AccessibilityDevice(bridge)` from the live Kotlin service via Chaquopy) pass it
straight to `AndroidBackend(config, device=...)` and never touch this. This
helper only supplies the *default* when no device is handed in — the PC path —
and it imports uiautomator2 lazily so merely importing the backend on a phone
(where uiautomator2 isn't installed) doesn't fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing device impls at module load
    from .base import Device
    from .accessibility_device import AccessibilityBridge
    from ..config import AppConfig


def build_device(config: "AppConfig", bridge: "AccessibilityBridge | None" = None) -> "Device":
    """Return the device to drive Instagram with.

    * `bridge` given  → `AccessibilityDevice` (on-device, no PC). This is the
      V3 path; the app passes the live accessibility service as the bridge.
    * `bridge` is None → `U2Device` (uiautomator2/ADB, needs a PC). V2 default.
    """
    if bridge is not None:
        from .accessibility_device import AccessibilityDevice
        return AccessibilityDevice(bridge)
    from .u2_device import U2Device
    return U2Device(config.device_serial)
