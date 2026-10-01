"""Unit tests for shared.scopes and the keys:manage creation guard."""

from __future__ import annotations

from app.schemas.dto.requests.api_key import ALLOWED_SCOPES, ApiKeyScope
from app.shared.scopes import (
    LEGACY_FULL_ACCESS_DESCRIPTION,
    SCOPE_DESCRIPTIONS,
    describe_scopes,
)


class TestScopeDescriptions:
    def test_every_scope_has_a_description(self):
        """New enum members must ship consent copy — no silent gaps."""
        assert set(SCOPE_DESCRIPTIONS) == set(ApiKeyScope)

    def test_descriptions_are_nonempty_sentences(self):
        for description in SCOPE_DESCRIPTIONS.values():
            assert description.strip()

    def test_keys_manage_copy(self):
        assert (
            SCOPE_DESCRIPTIONS[ApiKeyScope.KEYS_MANAGE]
            == "List and revoke your API keys"
        )

    def test_legacy_sentence_reads_as_full_access(self):
        assert "Full access" in LEGACY_FULL_ACCESS_DESCRIPTION


class TestDescribeScopes:
    def test_maps_slugs_in_order(self):
        assert describe_scopes(["urls:read", "shorten:create"]) == [
            "List and read links",
            "Create short links",
        ]

    def test_accepts_enum_members(self):
        assert describe_scopes([ApiKeyScope.STATS_READ]) == ["Read analytics data"]

    def test_unknown_slug_falls_back_to_raw(self):
        assert describe_scopes(["future:scope"]) == ["future:scope"]


class TestAllowedScopes:
    def test_keys_manage_not_creatable_on_api_keys(self):
        assert ApiKeyScope.KEYS_MANAGE not in ALLOWED_SCOPES

    def test_all_other_scopes_creatable(self):
        assert frozenset(ApiKeyScope) - {ApiKeyScope.KEYS_MANAGE} == ALLOWED_SCOPES
