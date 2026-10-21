"""DomainProfile: the evidence layer's domain knowledge, declaratively.

The architecture (retrieval → pack → decide → generate → validate) is
domain-agnostic; everything it knows about a domain — course-code shapes,
semester calendars, fold-family row formats, unit vocabularies, the
generator persona — lives in a YAML profile.  Swapping knowledge bases
means swapping one file (DOMAIN_PROFILE env), not source code.

Consumers keep their constant NAMES and read them through the profile at
import time, so migrating knowledge out of code changes no code paths.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:  # PyYAML is a builder-venv dependency; keep the error actionable.
    import yaml
except ImportError as error:  # pragma: no cover
    raise ImportError("DomainProfile needs PyYAML (pip install pyyaml)") from error

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = REPO_ROOT / "profiles" / "zjui_academic.yaml"

_FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X}


class DomainProfile:
    """Compiled view over one profile YAML (regex cache per instance)."""

    def __init__(self, data: dict[str, Any], path: str = "") -> None:
        self._data = data or {}
        self.path = path
        self.name = str((self._data.get("meta") or {}).get("name") or "unnamed")
        self._regex: dict[tuple[str, str, int], re.Pattern[str]] = {}

    # -- sections ---------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name)
        return value if isinstance(value, dict) else {}

    def text(self, section: str, key: str, default: str = "") -> str:
        return str(self.section(section).get(key, default))

    def words(self, section: str, key: str) -> list[str]:
        value = self.section(section).get(key)
        return [str(item) for item in value] if isinstance(value, list) else []

    def mapping(self, section: str, key: str) -> dict[str, Any]:
        value = self.section(section).get(key)
        return dict(value) if isinstance(value, dict) else {}

    def entries(self, key: str) -> list[dict[str, Any]]:
        """List-valued top-level sections (e.g. fold_families)."""
        value = self._data.get(key)
        return [dict(entry) for entry in value] if isinstance(value, list) else []

    # -- patterns ---------------------------------------------------------
    def pattern(self, section: str, key: str) -> re.Pattern[str]:
        """Compile (and cache) one pattern.  YAML form is either a plain
        string (no flags) or ``{pattern: ..., flags: "i|m"}``."""
        cached = self._regex.get((section, key, 0))
        if cached is not None:
            return cached
        raw = self.section(section).get(key)
        if raw is None:
            raise KeyError(f"profile[{self.name}].{section}.{key}: missing pattern")
        flags = 0
        if isinstance(raw, dict):
            for token in str(raw.get("flags", "")).lower().split("|"):
                flags |= _FLAG_MAP.get(token.strip(), 0)
            raw = raw.get("pattern")
        compiled = re.compile(str(raw), flags)
        self._regex[(section, key, 0)] = compiled
        return compiled

    def patterns(self, section: str, key: str) -> list[re.Pattern[str]]:
        return [self.pattern(section, f"{key}[{index}]") for index in range(len(self.section(section).get(key) or []))]


@lru_cache(maxsize=4)
def load_profile(path: str = "") -> DomainProfile:
    resolved = Path(path) if path else Path(os.environ.get("DOMAIN_PROFILE", "") or DEFAULT_PROFILE)
    data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return DomainProfile(data if isinstance(data, dict) else {}, str(resolved))
