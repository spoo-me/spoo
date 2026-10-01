"""
Scope descriptions — the one backend source for consent-screen copy.

Every ApiKeyScope maps to one plain-English sentence. The frontend keeps
a mirrored copy (spoo-landing components/dashboard/scopes.ts) so scope
chips and the consent screen say the same thing.
"""

from __future__ import annotations

from app.schemas.dto.requests.api_key import ApiKeyScope

SCOPE_DESCRIPTIONS: dict[ApiKeyScope, str] = {
    ApiKeyScope.SHORTEN_CREATE: "Create short links",
    ApiKeyScope.URLS_READ: "List and read links",
    ApiKeyScope.URLS_MANAGE: "Edit and delete links",
    ApiKeyScope.STATS_READ: "Read analytics data",
    ApiKeyScope.DOMAINS_READ: "List custom domains",
    ApiKeyScope.DOMAINS_MANAGE: "Add and remove domains",
    ApiKeyScope.REPORTS_CREATE: "Submit abuse reports",
    ApiKeyScope.WEBHOOKS_MANAGE: "Create and manage webhook endpoints",
    ApiKeyScope.WEBHOOKS_READ: "List webhook endpoints and delivery logs",
    ApiKeyScope.KEYS_MANAGE: "List and revoke your API keys",
    ApiKeyScope.ADMIN_ALL: "Full access, overrides all scopes",
}

# Shown for legacy grants that predate scoped consent — the grant is
# full-account and must never read as "no access".
LEGACY_FULL_ACCESS_DESCRIPTION = "Full access to your spoo.me account"


def describe_scopes(scopes: list[ApiKeyScope] | list[str]) -> list[str]:
    """Map scope slugs to their consent sentences, preserving order.

    Unknown slugs fall back to the raw slug so a stale grant snapshot
    never renders an empty permission list.
    """
    out: list[str] = []
    for scope in scopes:
        try:
            out.append(SCOPE_DESCRIPTIONS[ApiKeyScope(scope)])
        except (ValueError, KeyError):
            out.append(str(scope))
    return out
