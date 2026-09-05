"""
PATCH /api/v1/urls/{url_id}        — update URL properties
PATCH /api/v1/urls/{url_id}/status — update URL status only
DELETE /api/v1/urls/{url_id}       — delete a URL (returns 200)

All endpoints require authentication.  API key users require
``urls:manage`` or ``admin:all`` scope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from dependencies import (
    URL_MANAGEMENT_SCOPES,
    CurrentUser,
    CustomDomainSvc,
    Entitled,
    FeatureFlagSvc,
    Settings,
    TagSvc,
    UrlSvc,
    require_scopes,
)
from middleware.openapi import AUTH_RESPONSES, ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter, plan_scaled
from routes.api_v1._helpers import parse_url_id
from schemas.dto.requests.url import (
    ClaimUrlsRequest,
    UpdateUrlRequest,
    UpdateUrlStatusRequest,
)
from schemas.dto.responses.url import (
    ClaimResultItem,
    ClaimUrlsResponse,
    DeleteUrlResponse,
    UpdateUrlResponse,
)
from services.feature_flag_service import (
    AB_TESTING_FLAG,
    EXPIRED_FALLBACK_FLAG,
    GEO_TARGETING_FLAG,
    LINK_SCHEDULING_FLAG,
    META_TAGS_FLAG,
)
from shared.ip_utils import get_client_ip

router = APIRouter(tags=["Link Management"])


@router.post(
    "/urls/claim",
    responses=AUTH_RESPONSES,
    operation_id="claimUrls",
    summary="Claim anonymous URLs",
)
@limiter.limit(Limits.URL_CLAIM)
async def claim_urls_v1(
    request: Request,
    body: ClaimUrlsRequest,
    url_service: UrlSvc,
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> ClaimUrlsResponse:
    """Claim anonymously-created URLs into your account.

    Each item pairs a URL id with the one-time `claim_token` returned by
    the anonymous shorten call — the bearer proof of creation. Items
    resolve independently and the batch never hard-fails: `claimed`
    (ownership transferred, token burned), `already_yours` (idempotent
    repeat), or `invalid` (unknown id, wrong token, or a link that is not
    claimable — deliberately indistinguishable).

    The claim_token is single-use and is invalidated immediately on
    success. Claimed links join the account like any owned link:
    dashboard, management, and full stats history included.

    **Authentication**: Required.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 30/min, 500/day
    """
    results = await url_service.claim(body.claims, user.user_id)
    return ClaimUrlsResponse(
        results=[ClaimResultItem(url_id=r.url_id, status=r.status) for r in results],
        claimed=sum(1 for r in results if r.status == "claimed"),
    )


@router.patch(
    "/urls/{url_id}",
    responses=AUTH_RESPONSES,
    operation_id="updateUrl",
    summary="Update URL",
)
@limiter.limit(plan_scaled(Limits.URL_MANAGE))
async def update_url_v1(
    request: Request,
    url_id: Annotated[
        str,
        Path(
            description="Unique identifier of the URL",
            min_length=24,
            max_length=24,
            pattern=r"^[0-9a-f]{24}$",
        ),
    ],
    body: UpdateUrlRequest,
    url_service: UrlSvc,
    tag_service: TagSvc,
    custom_domain_service: CustomDomainSvc,
    settings: Settings,
    flag_svc: FeatureFlagSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> UpdateUrlResponse:
    """Update an existing URL's properties.

    Partially update a shortened URL. Only provided fields are modified; omitted
    fields remain unchanged. Pass `null` to remove optional settings like
    `password`, `max_clicks`, or `expire_after`.

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 120/min, 2,000/day

    **Updatable Fields**: `long_url`, `alias`, `password`, `block_bots`,
    `max_clicks`, `expire_after`, `private_stats`, `status`, `domain`,
    `geo_rules`, `ab_variants`, `expired_redirect_url`, `meta_tags`

    **Notes**:

    - Setting `max_clicks` to `0` or `null` removes the click limit
    - Changing the `alias` checks availability and may fail with 409 Conflict
    - Setting `domain` moves the URL to a different tenant; caller must own
      the target as an ACTIVE custom domain, or pass `null` to move back to
      the system default. Alias collision is verified on the target.
    - `geo_rules` replaces the whole map; pass `null` or `{}` to remove all
      rules
    - `ab_variants` replaces the whole list; pass `null` or `[]` to remove all
      variants
    - `expired_redirect_url` is where visitors land once the link has expired;
      pass `null` to go back to the expired page
    - The `url_id` is the MongoDB ObjectId, not the alias
    """
    oid = parse_url_id(url_id)
    # Setting geo rules is flag-gated; clearing (null/{}) is always allowed so
    # de-allowlisted owners can remove their rules during rollback.
    if "geo_rules" in body.model_fields_set and body.geo_rules:
        await flag_svc.require(GEO_TARGETING_FLAG, user, entitlements=entitlements)
    if "ab_variants" in body.model_fields_set and body.ab_variants:
        await flag_svc.require(AB_TESTING_FLAG, user, entitlements=entitlements)
    if "expired_redirect_url" in body.model_fields_set and body.expired_redirect_url:
        await flag_svc.require(EXPIRED_FALLBACK_FLAG, user, entitlements=entitlements)
    # Same deal for meta_tags: setting/replacing is flag-gated, clearing
    # (null) never is.
    if "meta_tags" in body.model_fields_set and body.meta_tags is not None:
        await flag_svc.require(META_TAGS_FLAG, user, entitlements=entitlements)
    if body.starts_at is not None or body.pre_start_url:
        await flag_svc.require(LINK_SCHEDULING_FLAG, user, entitlements=entitlements)
    # Verify domain ownership at the edge so the service can stay opaque about
    # tenancy. `domain` field-set with null means "move to system default" —
    # no ownership check needed for the default namespace.
    if body.domain and body.domain != settings.system_default_domain:
        await custom_domain_service.assert_owned_and_active(user, body.domain)
    doc = await url_service.update(
        oid, body, user.user_id, client_ip=get_client_ip(request)
    )
    refs = await tag_service.refs_by_id(user.user_id, doc.tag_ids)
    return UpdateUrlResponse.from_doc(doc, refs)


@router.patch(
    "/urls/{url_id}/status",
    responses=ERROR_RESPONSES,
    operation_id="updateUrlStatus",
    summary="Update URL Status",
)
@limiter.limit(plan_scaled(Limits.URL_MANAGE))
async def update_url_status_v1(
    request: Request,
    url_id: Annotated[
        str,
        Path(
            description="Unique identifier of the URL",
            min_length=24,
            max_length=24,
            pattern=r"^[0-9a-f]{24}$",
        ),
    ],
    body: UpdateUrlStatusRequest,
    url_service: UrlSvc,
    tag_service: TagSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> UpdateUrlResponse:
    """Update only the status of a URL (ACTIVE / INACTIVE).

    Toggle a URL between active and inactive without modifying other properties.

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 120/min, 2,000/day

    **Status Values** (user-editable via this endpoint):

    - `ACTIVE` — URL is accessible and redirects normally
    - `INACTIVE` — URL is disabled and returns an error page

    **Note**: `BLOCKED` is an admin-set status — blocked URLs cannot be modified
    or deleted by the owner. `EXPIRED` URLs (auto-set on max clicks or expiry
    time) can be reactivated by setting status back to `ACTIVE` — but only
    after the expiry condition is lifted (raise/clear `max_clicks`, extend/
    clear `expire_after` via PATCH), otherwise the URL immediately reads
    as expired again.

    **Use Cases**:

    - Set `INACTIVE` to temporarily disable redirects without deleting the URL
    - Set `ACTIVE` to re-enable a previously disabled URL
    """
    oid = parse_url_id(url_id)

    status_only = UpdateUrlRequest(status=body.status)

    doc = await url_service.update(oid, status_only, user.user_id)
    refs = await tag_service.refs_by_id(user.user_id, doc.tag_ids)
    return UpdateUrlResponse.from_doc(doc, refs)


@router.delete(
    "/urls/{url_id}",
    responses=ERROR_RESPONSES,
    operation_id="deleteUrl",
    summary="Delete URL",
)
@limiter.limit(plan_scaled(Limits.URL_DELETE))
async def delete_url_v1(
    request: Request,
    url_id: Annotated[
        str,
        Path(
            description="Unique identifier of the URL",
            min_length=24,
            max_length=24,
            pattern=r"^[0-9a-f]{24}$",
        ),
    ],
    url_service: UrlSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> DeleteUrlResponse:
    """Delete a URL permanently.

    **This action is IRREVERSIBLE.** The URL and its alias are permanently
    deleted, and the alias may be reclaimed by another user afterward.
    Historical click analytics are not removed; they are erased when the
    owning account is deleted.

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 60/min, 1,000/day

    **Recommendation**: Consider setting the URL status to `INACTIVE` via
    `PATCH /urls/{url_id}/status` instead if you may want to restore it later.
    """
    oid = parse_url_id(url_id)
    await url_service.delete(oid, user.user_id)
    return DeleteUrlResponse(message="URL deleted", id=url_id)
