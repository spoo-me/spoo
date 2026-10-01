"""Link tag normalisation, shared by the URL model and every DTO that
accepts tags so the rules cannot fork between create, patch and bulk."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

TAG_MAX_LENGTH = 32
TAGS_MAX_PER_LINK = 10

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_TAG_PUNCTUATION = frozenset(" -_.")


def _is_combining_mark(ch: str) -> bool:
    # Vowel signs in Indic scripts are marks, not letters, yet part of the word.
    return unicodedata.category(ch) in ("Mn", "Mc")


def normalise_tag(value: Any) -> str:
    """One tag: trim, casefold, collapse whitespace, whitelist characters.

    Raises ValueError so callers inside Pydantic validators surface a 422.
    """
    if not isinstance(value, str):
        raise ValueError("tags must be strings")
    tag = _CONTROL_CHARS_RE.sub("", _WHITESPACE_RE.sub(" ", value)).strip().casefold()
    if not tag:
        raise ValueError("tags cannot be empty")
    if len(tag) > TAG_MAX_LENGTH:
        raise ValueError(
            f"tag '{tag[:TAG_MAX_LENGTH]}…' exceeds {TAG_MAX_LENGTH} characters"
        )
    for ch in tag:
        if not (ch.isalnum() or ch in _TAG_PUNCTUATION or _is_combining_mark(ch)):
            raise ValueError(
                f"tag '{tag}' may only contain letters, digits, spaces, '-', '_' and '.'"
            )
    return tag


def normalise_tags(values: Any, *, cap: int | None = TAGS_MAX_PER_LINK) -> list[str]:
    """Normalise a tag list: per-item rules, dedupe (first wins), cap count.

    ``None`` means "no tags" and returns ``[]``. Non-list input is returned
    untouched so Pydantic rejects it with its own type error. ``cap`` is the
    per-link assignment limit; filters pass ``None`` since they assign nothing.
    """
    if values is None:
        return []
    if not isinstance(values, list):
        return values
    seen: set[str] = set()
    tags: list[str] = []
    for value in values:
        tag = normalise_tag(value)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    if cap is not None and len(tags) > cap:
        raise ValueError(f"a link can carry at most {cap} tags")
    return tags
