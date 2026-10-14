"""Explicit caller-ID mapping; caller ID is attribution, not authentication."""

from __future__ import annotations

import json
import re
from pathlib import Path


def normalize_caller(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("+"):
        digits = digits[1:]
    # Egyptian national mobile numbers and +20 country format are equivalent.
    if len(digits) == 11 and digits.startswith("01"):
        return "20" + digits[1:]
    return digits


class CallerDirectory:
    def __init__(self, mappings: dict[str, str], people_keys: set[str]):
        self._mappings: dict[str, str] = {}
        for number, key in mappings.items():
            normalized = normalize_caller(number)
            if not normalized or key not in people_keys:
                raise ValueError(f"invalid caller mapping for {number!r}")
            if normalized in self._mappings and self._mappings[normalized] != key:
                raise ValueError(f"conflicting caller mapping for {number!r}")
            self._mappings[normalized] = key

    @classmethod
    def load(cls, path: str, people_keys: set[str]) -> CallerDirectory:
        mappings = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        if not isinstance(mappings, dict):
            raise ValueError("CALLER_DIRECTORY_FILE must contain a JSON object")
        return cls(mappings, people_keys)

    def lookup(self, number: str) -> str | None:
        return self._mappings.get(normalize_caller(number))
