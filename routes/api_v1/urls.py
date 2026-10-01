"""
GET    /api/v1/urls                    — list authenticated user's shortened URLs
GET    /api/v1/urls/{url_id}           — fetch one owned URL by ObjectId
GET    /api/v1/urls/{domain}/{alias}   — fetch one owned URL by its natural key
DELETE /api/v1/urls?domain=            — bulk-delete URLs scoped to one custom domain

Requires authentication. Listing returns paginated list with camelCase keys.
Single-resource GETs serve the exact same item shape the list serves per
element, are status-blind for the owner, and answer 404 for foreign links
(ownership is in the query — no existence oracle).
Bulk delete is owner-scoped and refuses the system-default namespace.

Route-ordering note: FastAPI matches routes in definition order. The two
single-resource GETs cannot shadow each other (different segment counts)
nor the management routes on the same shapes (those are PATCH/DELETE —
method filtering keeps them apart), and the literal ``/urls`` list/bulk
routes never match a subpath.

Namespace note: ``GET /urls/{domain}/{alias}`` claims the ENTIRE two-segment
GET namespace under ``/urls``. Any future sibling GET of the shape
``/urls/x/y`` (e.g. ``GET /urls/{url_id}/stats``) must be declared in this
module ABOVE this route, or take a different shape — otherwise it is
silently shadowed here (the first segment parses as ``domain``, the second
as ``alias``).
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Path, Query, Request

from dependencies import (
    URL_MANAGEMENT_SCOPES,
    URL_READ_SCOPES,
    CurrentUser,
    CustomDomainSvc,
    Entitled,
    Settings,
    TagSvc,
    UrlSvc,
    require_scopes,
)
from errors import NotFoundError, ValidationError
from middleware.openapi import AUTH_RESPONSES, ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter, plan_scaled
from routes.api_v1._helpers import parse_url_id
from schemas.dto.requests.url import ListUrlsQuery
from schemas.dto.responses.url import (
    BulkDeleteUrlsResponse,
    UrlListItem,
    UrlListResponse,
)
from shared.alias_dispatch import v2_lookup_code
from shared.url_utils import is_system_default_host, normalise_fqdn, normalise_host

router = APIRouter(tags=["Link Management"])


@router.get(
    "/urls",
    responses=ERROR_RESPONSES,
    operation_id="listUrls",
    summary="List Your URLs",
)
@limiter.limit(plan_scaled(Limits.API_AUTHED))
async def list_urls_v1(
    request: Request,
    entitlements: Entitled,
    query: Annotated[ListUrlsQuery, Query()],
    url_service: UrlSvc,
    tag_service: TagSvc,
    user: CurrentUser = Depends(require_scopes(URL_READ_SCOPES)),  # noqa: B008
) -> UrlListResponse:
    """List all URLs owned by the authenticated user.

    Returns a paginated list of shortened URLs with support for filtering,
    sorting, and full-text search on aliases and destination URLs.

    **Authentication**: Required.

    **API Key Scope**: `urls:manage`, `urls:read`, or `admin:all`

    **Rate Limits**: 60/min, 5,000/day

    **Pagination**: Use `page` and `pageSize` query params. Response includes
    `hasNext` boolean and `total` count.

    **Sorting**: Sort by `created_at`, `last_click`, or `total_clicks` in
    ascending or descending order.

    **Filtering**: Pass a JSON-encoded `filter` parameter with fields like
    `status`, `createdAfter`, `createdBefore`, `passwordSet`, `maxClicksSet`,
    `search`, `tagIds`, `tagNames` and `tagsMatch`.
    """
    result = await url_service.list_by_owner(user.user_id, query)
    refs = await tag_service.refs_by_id(
        user.user_id, [t for doc in result["items"] for t in doc.tag_ids]
    )
    result["items"] = [UrlListItem.from_doc(doc, refs) for doc in result["items"]]
    return result


@router.get(
    "/urls/{url_id}",
    responses=ERROR_RESPONSES,
    operation_id="getUrl",
    summary="Get URL by ID",
)
@limiter.limit(plan_scaled(Limits.API_AUTHED))
async def get_url_v1(
    request: Request,
    entitlements: Entitled,
    url_id: Annotated[
        str,
        Path(description="Unique identifier of the URL (MongoDB ObjectId)."),
    ],
    url_service: UrlSvc,
    tag_service: TagSvc,
    user: CurrentUser = Depends(require_scopes(URL_READ_SCOPES)),  # noqa: B008
) -> UrlListItem:
    """Fetch a single URL you own by its id.

    Returns the same item shape as one element of `GET /urls` — including
    derived `status`, so expired/disabled/blocked links return normally
    with their status field (you are reading your inventory, not following
    a redirect).

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `urls:manage`, `urls:read`, or `admin:all`

    **Rate Limits**: 60/min, 5,000/day

    **Errors**:

    - `400` — malformed id (not a valid ObjectId)
    - `404` — no URL with that id in your account. A URL owned by someone
      else answers identically; this endpoint never confirms foreign ids.
    """
    oid = parse_url_id(url_id)
    doc = await url_service.get_owned(oid, user.user_id)
    refs = await tag_service.refs_by_id(user.user_id, doc.tag_ids)
    return UrlListItem.from_doc(doc, refs)


def _resolve_lookup_domain(raw: str, system_default_domain: str) -> str:
    """Map a domain path segment to the stored domain key.

    Normalises the host (lowercase, port and trailing dot stripped — the
    same rules the tenant middleware applies to the Host header) and folds
    the system domain's names onto the canonical default-domain key via the
    shared ``is_system_default_host`` predicate — the same rule the tenant
    resolver's system-default short-circuit uses (system domain plus its
    ``www.`` alias). Anything else scopes the lookup to that custom domain.
    """
    host = normalise_host(raw)
    if not host:
        # Unparseable host — nothing can live there, same answer as unknown.
        raise NotFoundError("URL not found")
    if is_system_default_host(host, system_default_domain):
        return system_default_domain
    return host


@router.get(
    "/urls/{domain}/{alias}",
    responses=ERROR_RESPONSES,
    operation_id="getUrlByAddress",
    summary="Get URL by Domain and Alias",
)
@limiter.limit(plan_scaled(Limits.API_AUTHED))
async def get_url_by_address_v1(
    request: Request,
    entitlements: Entitled,
    domain: Annotated[
        str,
        Path(
            max_length=253,
            description=(
                "Domain the short link lives on. Always explicit: pass the "
                "system domain (e.g. `spoo.me`) for default-namespace links "
                "or an owned custom domain fqdn. Case and `:port` are "
                "ignored."
            ),
            examples=["spoo.me", "links.acme.com"],
        ),
    ],
    alias: Annotated[
        str,
        Path(
            description=(
                "Short code of the URL. Emoji aliases arrive percent-encoded."
            ),
            examples=["mylink"],
        ),
    ],
    url_service: UrlSvc,
    tag_service: TagSvc,
    settings: Settings,
    user: CurrentUser = Depends(require_scopes(URL_READ_SCOPES)),  # noqa: B008
) -> UrlListItem:
    """Fetch a single URL you own by its natural key: domain + alias.

    The address every short link is already known by — no id lookup
    round-trip needed. Returns the same item shape as one element of
    `GET /urls`, status-blind for the owner (expired/disabled/blocked
    links return with their status field).

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `urls:manage`, `urls:read`, or `admin:all`

    **Rate Limits**: 60/min, 5,000/day

    **Errors**:

    - `404` — no URL at that address in your account. Unknown domains and
      links owned by someone else answer identically; this endpoint never
      confirms what exists outside your account.

    **Note**: only current-generation links resolve here — that is all the
    managed collection holds.
    """
    # Canonicalize before the exact-match lookup: v2 stores emoji aliases in
    # canonical form (VS16 stripped), so a pasted VS16 variant must fold onto
    # the stored bare form or it 404s here while the redirect serves it. A
    # no-op for alphanumeric codes. Same seam every other read surface uses.
    short_code = v2_lookup_code(unquote(alias))
    lookup_domain = _resolve_lookup_domain(domain, settings.system_default_domain)
    doc = await url_service.get_owned_by_alias(
        short_code, user.user_id, domain=lookup_domain
    )
    refs = await tag_service.refs_by_id(user.user_id, doc.tag_ids)
    return UrlListItem.from_doc(doc, refs)


@router.delete(
    "/urls",
    responses=AUTH_RESPONSES,
    operation_id="bulkDeleteUrls",
    summary="Bulk Delete URLs on a Custom Domain",
)
@limiter.limit(Limits.URL_BULK_DELETE)
async def bulk_delete_urls_v1(
    request: Request,
    domain: Annotated[
        str,
        Query(
            min_length=1,
            max_length=253,
            description="Custom domain fqdn whose URLs should be deleted.",
            examples=["links.acme.com"],
        ),
    ],
    url_service: UrlSvc,
    custom_domain_service: CustomDomainSvc,
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> BulkDeleteUrlsResponse:
    """Bulk-delete all URLs the caller owns on the given custom domain.

    **Authentication**: Required.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 5/min, 50/day.

    **Restrictions**: refuses the system-default domain — protects against
    accidental nuke of the user's entire spoo.me URL inventory. Caller must
    own the domain.
    """
    try:
        fqdn = normalise_fqdn(domain)
    except ValueError as exc:
        raise ValidationError(str(exc), field="domain") from exc
    await custom_domain_service.assert_owned(user, fqdn)
    count = await url_service.delete_all_by_domain(user.user_id, fqdn)
    return BulkDeleteUrlsResponse(
        message=f"deleted {count} URL(s) on {fqdn}",
        count=count,
        domain=fqdn,
    )
