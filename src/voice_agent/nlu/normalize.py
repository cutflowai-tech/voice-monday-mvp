"""Arabic text normalization helpers.

Two levels are used:

* ``clean_text`` keeps the original spelling (only removes diacritics, tatweel and
  punctuation) so extracted task titles stay readable.
* ``normalize_token`` aggressively unifies letter variants for *matching* only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_PUNCT = re.compile(r"[،؛؟٫٬۔,.;:!?\"'«»()\[\]{}\-–—…/\\]+")
_SPACES = re.compile(r"\s+")

_LETTER_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
    }
)


def clean_text(text: str) -> str:
    text = _DIACRITICS.sub("", text).replace(_TATWEEL, "")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalize_token(token: str) -> str:
    token = _DIACRITICS.sub("", token).replace(_TATWEEL, "")
    return token.translate(_LETTER_MAP).lower()


def normalize_text(text: str) -> str:
    return " ".join(normalize_token(t) for t in clean_text(text).split())


@dataclass(frozen=True)
class Token:
    orig: str
    norm: str


def tokenize(text: str) -> list[Token]:
    return [Token(orig=t, norm=normalize_token(t)) for t in clean_text(text).split()]


def join_orig(tokens: list[Token]) -> str:
    return " ".join(t.orig for t in tokens)
