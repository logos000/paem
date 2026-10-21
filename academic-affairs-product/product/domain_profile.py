"""Product-side DomainProfile bridge.

The product consumes the SAME profile YAML the builder runtime uses
(query-side knowledge: generator persona, claim-validation units and
patterns).  Path bridge: PRODUCT_DOMAIN_PROFILE env, else the sibling
builder repo's profiles/ directory — the two repos already share code and
corpora by path bridge (Stage 3A single-path guarantee).

Kept deliberately thin; the loader contract mirrors the builder's
domain_profile.py so a profile file is interchangeable across repos.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT.parent / "llm-corpus-builder" / "profiles" / "zjui_academic.yaml"

_FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X}


class DomainProfile:
    def __init__(self, data: dict[str, Any], path: str = "") -> None:
        self._data = data or {}
        self.path = path
        self.name = str((self._data.get("meta") or {}).get("name") or "unnamed")
        self._regex: dict[tuple[str, str], re.Pattern[str]] = {}

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

    def pattern(self, section: str, key: str) -> re.Pattern[str]:
        cached = self._regex.get((section, key))
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
        self._regex[(section, key)] = compiled
        return compiled


@lru_cache(maxsize=4)
def load_profile(path: str = "") -> DomainProfile:
    resolved = Path(path) if path else Path(os.environ.get("PRODUCT_DOMAIN_PROFILE", "") or DEFAULT_PROFILE)
    data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return DomainProfile(data if isinstance(data, dict) else {}, str(resolved))
