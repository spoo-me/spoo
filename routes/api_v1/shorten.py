"""
POST /api/v1/shorten — create a shortened URL.

Returns 201 on success with the URL details.
Auth is optional; API key users require `shorten:create` or `admin:all` scope.

When ``domain`` is supplied the route layer asserts the caller owns an ACTIVE
custom domain with that fqdn before delegating to ``UrlService.create``. Short
URL is built from the custom host. Anonymous callers cannot specify ``domain``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from dependencies import (
    SHORTEN_SCOPES,
    CurrentUser,
    CustomDomainSvc,
    Entitled,
    FeatureFlagSvc,
    Settings,
    TagSvc,
    UrlSvc,
    optional_scopes_verified,
)
from errors import AuthenticationError
from middleware.openapi import AUTH_RESPONSES, OPTIONAL_AUTH_SECURITY
from middleware.rate_limiter import Limits, dynamic_limit, limiter
from schemas.dto.requests.url import AliasCheckQuery, CreateUrlRequest
from schemas.dto.responses.url import AliasCheckResponse, UrlResponse
from services.feature_flag_service import (
    AB_TESTING_FLAG,
    EXPIRED_FALLBACK_FLAG,
    GEO_TARGETING_FLAG,
    LINK_SCHEDULING_FLAG,
    META_TAGS_FLAG,
)
from shared.client_tag import CLIENT_TAG_HEADER, first_party_client
from shared.ip_utils import get_client_ip

router = APIRouter(tags=["URL Shortening"])

_shorten_limit, _shorten_key = dynamic_limit(Limits.API_AUTHED, Limits.API_ANON)
_check_limit, _check_key = dynamic_limit(Limits.API_CHECK_AUTHED, Limits.API_CHECK_ANON)


@router.post(
    "/shorten",
    status_code=201,
    responses=AUTH_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="shortenUrl",
    summary="Create Shortened URL",
)
@limiter.limit(_shorten_limit, key_func=_shorten_key)
async def shorten_v1(
    request: Request,
    body: CreateUrlRequest,
    url_service: UrlSvc,
    tag_service: TagSvc,
    custom_domain_service: CustomDomainSvc,
    settings: Settings,
    flag_svc: FeatureFlagSvc,
    entitlements: Entitled,
    user: CurrentUser | None = Depends(optional_scopes_verified(SHORTEN_SCOPES)),  # noqa: B008
) -> UrlResponse:
    """Create a new shortened URL.

    Create a shortened URL with optional customization including password
    protection, expiration, click limits, and bot blocking. Authenticated
    users may target an owned, ACTIVE custom domain via the ``domain`` field.

    **Emoji aliases**: ``alias`` also accepts an emoji-only short code
    (e.g. ``🚀🔥``) — 1-15 fully-qualified emoji; ZWJ sequences, flags,
    keycaps, and text-style symbols are rejected (they render or copy
    inconsistently across platforms). Set ``alias_type: "emoji"`` to
    auto-generate an emoji code instead of an alphanumeric one. Stored
    and returned in canonical form (variation selectors stripped).

    **Authentication**: Optional — higher rate limits when authenticated.
    Required if ``domain`` is supplied.

    **API Key Scope**: `shorten:create` or `admin:all`

    **Rate Limits**:

    - Authenticated: 60/min, 5,000/day
    - Anonymous: 20/min, 1,000/day

    **Anonymous Usage Consequences**:

    - Lower rate limits
    - Cannot manage or view URLs later
    - Cannot use private stats
    - URLs not linked to any account
    - Cannot use custom domains
    - Cannot use geo targeting
    - Cannot use A/B variants
    - Cannot set an expired-link fallback
    - Cannot use custom meta tags
    """
    owner_id = user.user_id if user is not None else None
    client_ip = get_client_ip(request)

    if body.geo_rules:
        if user is None:
            raise AuthenticationError("Authentication required to set geo_rules")
        await flag_svc.require(GEO_TARGETING_FLAG, user, entitlements=entitlements)

    if body.ab_variants:
        if user is None:
            raise AuthenticationError("Authentication required to set ab_variants")
        await flag_svc.require(AB_TESTING_FLAG, user, entitlements=entitlements)

    if body.expired_redirect_url:
        if user is None:
            raise AuthenticationError(
                "Authentication required to set expired_redirect_url"
            )
        await flag_svc.require(EXPIRED_FALLBACK_FLAG, user, entitlements=entitlements)

    # optional_scopes_verified already rejects unverified authenticated users,
    # so the flag is the only remaining gate here.
    if body.meta_tags is not None:
        if user is None:
            raise AuthenticationError("Authentication required to set meta_tags")
        await flag_svc.require(META_TAGS_FLAG, user, entitlements=entitlements)

    if body.starts_at is not None or body.pre_start_url:
        if user is None:
            raise AuthenticationError("Authentication required to schedule a link")
        await flag_svc.require(LINK_SCHEDULING_FLAG, user, entitlements=entitlements)

    if body.domain and body.domain != settings.system_default_domain:
        if user is None:
            raise AuthenticationError(
                "Authentication required to shorten on a custom domain"
            )
        await custom_domain_service.assert_owned_and_active(user, body.domain)
        base_url = f"https://{body.domain}"
        scoped_domain: str | None = body.domain
    else:
        base_url = settings.app_url
        scoped_domain = None

    created_via = first_party_client(request.headers.get(CLIENT_TAG_HEADER))
    doc, claim_token = await url_service.create(
        body, owner_id, client_ip, domain=scoped_domain, created_via=created_via
    )
    refs = (
        await tag_service.refs_by_id(owner_id, doc.tag_ids)
        if owner_id is not None and doc.tag_ids
        else None
    )
    return UrlResponse.from_doc(doc, base_url, claim_token=claim_token, tag_refs=refs)


@router.get(
    "/shorten/check-alias",
    responses=AUTH_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="checkAliasAvailability",
    summary="Check Alias Availability",
)
@limiter.limit(_check_limit, key_func=_check_key)
async def check_alias(
    request: Request,
    url_service: UrlSvc,
    custom_domain_service: CustomDomainSvc,
    settings: Settings,
    query: Annotated[AliasCheckQuery, Query()],
    user: CurrentUser | None = Depends(optional_scopes_verified(SHORTEN_SCOPES)),  # noqa: B008
) -> AliasCheckResponse:
    """Check whether a proposed alias would be accepted by POST /api/v1/shorten.

    Reason codes on a negative result (``length``/``format``/``reserved``/
    ``taken``/``emoji_policy``) let the UI render precise inline feedback
    without duplicating the validation rules. Emoji aliases are checked in
    canonical form (variation selectors stripped).

    Pass ``domain`` to scope the check to a custom-domain tenant — required
    for the create modal's live availability indicator when the user has
    picked a non-default domain. Authz mirrors the shorten endpoint: anon
    callers can't probe custom domains; authed callers must own the target.

    **Authentication**: Optional — higher rate limits when authenticated.
    Required if ``domain`` is supplied.
    """
    if query.domain and query.domain != settings.system_default_domain:
        if user is None:
            raise AuthenticationError(
                "Authentication required to check aliases on a custom domain"
            )
        await custom_domain_service.assert_owned_and_active(user, query.domain)
        scope: str | None = query.domain
    else:
        scope = None
    result = await url_service.check_alias(query.alias, domain=scope)
    return AliasCheckResponse(
        available=result == "available",
        reason=None if result == "available" else result,
    )
