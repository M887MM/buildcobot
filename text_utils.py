"""
Utilities for normalizing text that may have been decoded with the wrong
encoding (mojibake).
"""

from __future__ import annotations

import unicodedata
from typing import Iterable


CYRILLIC_EXTRA = {"Ё", "ё", "І", "і", "Ї", "ї", "Ґ", "ґ", "Є", "є"}


def _score_text(text: str) -> int:
    if not text:
        return -10

    cyrillic = sum(
        1
        for ch in text
        if "А" <= ch <= "я" or ch in CYRILLIC_EXTRA or "\u0400" <= ch <= "\u04FF"
    )
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    digits = sum(1 for ch in text if ch.isdigit())
    replacements = text.count("\ufffd") + text.count("?")
    control = sum(
        1
        for ch in text
        if unicodedata.category(ch) in {"Cc", "Cf"} and ch not in ("\n", "\r", "\t")
    )

    return cyrillic * 4 + latin + digits - (replacements + control) * 5


def _generate_candidates(text: str) -> Iterable[str]:
    yield text
    enc_pairs = [
        ("latin-1", "utf-8"),
        ("latin-1", "cp1251"),
        ("latin-1", "koi8-r"),
        ("cp1251", "utf-8"),
        ("cp1251", "koi8-r"),
        ("cp1251", "cp866"),
        ("koi8-r", "utf-8"),
        ("cp866", "utf-8"),
        ("cp866", "cp1251"),
    ]
    for source, target in enc_pairs:
        try:
            candidate = text.encode(source, errors="ignore").decode(target, errors="ignore")
        except Exception:
            continue
        if candidate:
            yield candidate


def fix_mojibake(text: str | None) -> str | None:
    """
    Tries a set of encoding round-trips and returns the variant with the
    highest share of Cyrillic/ASCII characters. Returns the input unchanged
    if no better candidate is found.
    """
    if not text or not isinstance(text, str):
        return text

    best = text
    best_score = _score_text(text)

    for candidate in _generate_candidates(text):
        score = _score_text(candidate)
        if score > best_score:
            best = candidate
            best_score = score

    return best


def normalize_text(text: str | None) -> str | None:
    """Public helper to clean text coming from the database."""
    if not text:
        return text
    return fix_mojibake(text.strip())


def format_price(value: float | int | None, suffix: str = "сум") -> str | None:
    """
    Formats numeric price with thin spaces between thousands.
    Returns None if value is not provided.
    """
    if value is None:
        return None
    try:
        normalized = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    formatted = f"{normalized:,}".replace(",", " ")
    return f"{formatted} {suffix}".strip()
