"""Read the same translation catalogs used by AppKit, without a second copy."""
from __future__ import annotations

import json
import locale
import os
from pathlib import Path
import re
import tempfile


def resources() -> Path:
    return Path(__file__).resolve().parents[2] / "Resources"


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "4dgs-studio" / "preferences.json"


def read_preferences() -> dict:
    try:
        result = json.loads(config_path().read_text())
        if not isinstance(result, dict):
            return {}
        return {"language": result.get("language") if result.get("language") in ("system", "en", "zh-Hans") else "system",
                "appearance": result.get("appearance") if type(result.get("appearance")) is int and result.get("appearance") in (0, 1, 2) else 0}
    except (OSError, ValueError):
        return {}


def save_preferences(values: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".preferences-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Translator:
    def __init__(self, language=None):
        language = language or read_preferences().get("language", "system")
        if language == "system":
            language = os.environ.get("LANGUAGE", "").split(":")[0] or locale.getlocale()[0] or "en"
        self.language = "zh-Hans" if language.startswith("zh") else "en"
        self.catalog = {}
        path = resources() / (self.language + ".lproj") / "Localizable.strings"
        for key, value in re.findall(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', path.read_text()):
            self.catalog[json.loads('"' + key + '"')] = json.loads('"' + value + '"')

    def __call__(self, key, fallback=None):
        # macOS shortcut symbols are adapted only in UI text, never in document values.
        return self.catalog.get(key, fallback or key).replace("⌘", "Ctrl+").replace("Option", "Alt").replace("Command-click", "Ctrl-click")


tr = Translator()
