"""Reserved alias list — shared/reserved_aliases.py."""

from __future__ import annotations

from app.shared.reserved_aliases import RESERVED_ALIASES, is_reserved_alias
from app.shared.validators import validate_alias


def test_frontend_surfaces_are_reserved():
    for alias in (
        "about",
        "pricing",
        "onboarding",
        "dashboard",
        "relay",
        "terms",
        "tools",
    ):
        assert is_reserved_alias(alias)


def test_matching_is_case_insensitive():
    assert is_reserved_alias("About")
    assert is_reserved_alias("PRICING")
    assert is_reserved_alias("Terms-Of-Service")


def test_ordinary_aliases_pass():
    for alias in ("my-link", "abc123", "promo2026"):
        assert not is_reserved_alias(alias)


def test_every_entry_is_normalized_and_alias_shaped():
    # The set must stay lowercase (the check lowercases input, so a
    # mixed-case entry would silently never match) and within the alias
    # charset — an entry that can't be requested guards nothing.
    for entry in RESERVED_ALIASES:
        assert entry == entry.lower()
        assert validate_alias(entry)
