"""Load/save the user profile, tunable settings, and the enabled-chat list.

All state lives in a single JSON file (default: ./data/config.json) so it is
easy to inspect and edit by hand.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .models import Profile, Settings


DEFAULT_CONFIG_PATH = os.path.join("data", "config.json")


@dataclass
class AppConfig:
    profile: Profile = field(default_factory=Profile)
    settings: Settings = field(default_factory=Settings)
    # Conversation names/titles exactly as they appear in the Instagram inbox.
    enabled_chats: list[str] = field(default_factory=list)
    # Android device serial (adb). Empty => first/only device.
    device_serial: str = ""
    # ntfy.sh topic to push error/unsure notifications to from the web UI.
    # Empty => notifications are skipped (printed to the server log instead).
    ntfy_topic: str = ""

    def to_dict(self) -> dict:
        return {
            "profile": self.profile.to_dict(),
            "settings": self.settings.to_dict(),
            "enabled_chats": self.enabled_chats,
            "device_serial": self.device_serial,
            "ntfy_topic": self.ntfy_topic,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        return cls(
            profile=Profile.from_dict(d.get("profile", {})),
            settings=Settings.from_dict(d.get("settings", {})),
            enabled_chats=list(d.get("enabled_chats", [])),
            device_serial=d.get("device_serial", ""),
            ntfy_topic=d.get("ntfy_topic", ""),
        )


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not os.path.exists(path):
        return AppConfig()
    with open(path, "r", encoding="utf-8") as f:
        return AppConfig.from_dict(json.load(f))


def save_config(config: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)


def config_exists(path: str = DEFAULT_CONFIG_PATH) -> bool:
    return os.path.exists(path)
