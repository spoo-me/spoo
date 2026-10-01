"""
POST   /api/v1/custom-domains              — register a new custom domain
POST   /api/v1/custom-domains/{id}/verify  — trigger verification
GET    /api/v1/custom-domains              — list owned domains (paginated)
GET    /api/v1/custom-domains/{id}         — fetch a single domain
PATCH  /api/v1/custom-domains/{id}         — update per-domain routing config
DELETE /api/v1/custom-domains/{id}         — revoke (?cascade=true also deletes URLs)

CREATE is gated on a verified email + the ``custom_domains`` feature flag.
Read/verify/delete bypass the flag so existing owners can manage state during
rollback.

API key scopes: ``domains:manage`` (create/verify/delete/update) and
``domains:read`` (list/get). JWT bearer works for any operation without scopes.
"""

from __future__ import annotations

from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, Path, Query, Request

from dependencies import (
    DOMAIN_MANAGE_SCOPES,
    DOMAIN_READ_SCOPES,
    CurrentUser,
    CustomDomainSvc,
    Entitled,
    FeatureFlagSvc,
    require_scopes,
    require_scopes_verified,
)
from errors import NotFoundError, PlanRequiredError
from middleware.openapi import AUTH_RESPONSES, ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.custom_domain import (
    CreateCustomDomainRequest,
    ListCustomDomainsQuery,
    UpdateCustomDomainRequest,
)
from schemas.dto.responses.custom_domain import (
    CustomDomainDeleteResponse,
    CustomDomainListResponse,
    CustomDomainResponse,
)
from services.entitlements import Resolved
from services.feature_flag_service import CUSTOM_DOMAINS_FLAG

router = APIRouter(tags=["Custom Domains"])

_FEATURE_FLAG = CUSTOM_DOMAINS_FLAG


def _parse_domain_id(domain_id: str) -> ObjectId:
    """Parse the path param to ObjectId or raise 404.

    404 (not 400) hides existence of the feature for non-allowlisted users
    poking the endpoint with bogus IDs."""
    try:
        return ObjectId(domain_id)
    except Exception:
        raise NotFoundError("domain not found") from None


async def _require_create_enabled(
    flag_svc: FeatureFlagSvc, user: CurrentUser, entitlements: Resolved
) -> None:
    """Flag off: 404, so the feature's existence never leaks. Flag on but
    not in the plan: 403 ``plan_required``, the client already sees LOCKED."""
    await flag_svc.require(_FEATURE_FLAG, user, entitlements=entitlements, hide=True)


@router.post(
    "/custom-domains",
    status_code=201,
    responses=AUTH_RESPONSES,
    operation_id="createCustomDomain",
    summary="Register Custom Domain",
)
@limiter.limit(Limits.DOMAIN_CREATE)
async def create_custom_domain(
    request: Request,
    body: CreateCustomDomainRequest,
    service: CustomDomainSvc,
    flag_svc: FeatureFlagSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes_verified(DOMAIN_MANAGE_SCOPES)),  # noqa: B008
) -> CustomDomainResponse:
    """Register a new custom domain for branded short links.

    The domain is born in `PENDING` state. The user must publish the DNS
    records returned in `dns_records` at their DNS provider, then call
    `POST /custom-domains/{id}/verify` to trigger verification. Cloudflare
    auto-verifies via HTTP DCV once the CNAME is live and propagated.

    **Authentication**: Required (JWT Bearer or API key with `domains:manage`).

    **Email verification**: Required (applies to API key callers too).

    **Feature gate**: Must be enabled for the calling user.

    **Rate Limits**: 10/hour (route, failed attempts count). Owned domains
    (any status) count against the plan's `custom_domains_max`; a full
    limit answers 403 `limit_reached`.
    """
    await _require_create_enabled(flag_svc, user, entitlements)
    doc = await service.create(
        body, user, max_domains=entitlements.limit("custom_domains_max")
    )
    return CustomDomainResponse.from_doc(doc)


@router.post(
    "/custom-domains/{domain_id}/verify",
    responses=AUTH_RESPONSES,
    operation_id="verifyCustomDomain",
    summary="Verify Custom Domain",
)
@limiter.limit(Limits.DOMAIN_VERIFY)
async def verify_custom_domain(
    request: Request,
    domain_id: Annotated[str, Path(description="MongoDB ObjectId of the domain.")],
    service: CustomDomainSvc,
    user: CurrentUser = Depends(require_scopes(DOMAIN_MANAGE_SCOPES)),  # noqa: B008
) -> CustomDomainResponse:
    """Trigger a fresh verification check for a custom domain.

    The verifier dispatched depends on the chosen DCV method (CF HTTP DCV on
    CF SaaS deployments, CNAME/A/TXT on self-host). On success the domain
    transitions to `ACTIVE` and short links on the domain start resolving.

    **Authentication**: Required (JWT or API key with `domains:manage`).

    **Rate Limits**: 10/min (route) + 60/hour per domain (service quota).
    """
    oid = _parse_domain_id(domain_id)
    doc = await service.verify(oid, user)
    return CustomDomainResponse.from_doc(doc)


@router.get(
    "/custom-domains",
    responses=ERROR_RESPONSES,
    operation_id="listCustomDomains",
    summary="List Custom Domains",
)
@limiter.limit(Limits.DOMAIN_READ)
async def list_custom_domains(
    request: Request,
    query: Annotated[ListCustomDomainsQuery, Query()],
    service: CustomDomainSvc,
    user: CurrentUser = Depends(require_scopes(DOMAIN_READ_SCOPES)),  # noqa: B008
) -> CustomDomainListResponse:
    """List custom domains owned by the authenticated user.

    Reads bypass the feature flag so owners can still see state during a
    rollback.

    **Authentication**: Required (JWT or API key with `domains:read`).

    **Rate Limits**: 60/min.

    **Pagination**: `page` (default 1) + `pageSize` (default 20, max 100).
    """
    items, total = await service.list_by_owner(user, query)
    has_next = query.page * query.page_size < total
    return CustomDomainListResponse(
        items=[CustomDomainResponse.from_doc(doc) for doc in items],
        page=query.page,
        pageSize=query.page_size,
        total=total,
        hasNext=has_next,
    )


@router.get(
    "/custom-domains/{domain_id}",
    responses=ERROR_RESPONSES,
    operation_id="getCustomDomain",
    summary="Get Custom Domain",
)
@limiter.limit(Limits.DOMAIN_READ)
async def get_custom_domain(
    request: Request,
    domain_id: Annotated[str, Path(description="MongoDB ObjectId of the domain.")],
    service: CustomDomainSvc,
    user: CurrentUser = Depends(require_scopes(DOMAIN_READ_SCOPES)),  # noqa: B008
) -> CustomDomainResponse:
    """Fetch a single custom domain owned by the caller.

    Used by the dashboard detail view + auto-poll after Verify. Bypasses the
    feature flag like other read endpoints.

    **Authentication**: Required (JWT or API key with `domains:read`).

    **Rate Limits**: 60/min.
    """
    oid = _parse_domain_id(domain_id)
    doc = await service.get_owned_by_id(oid, user)
    return CustomDomainResponse.from_doc(doc)


@router.patch(
    "/custom-domains/{domain_id}",
    responses=AUTH_RESPONSES,
    operation_id="updateCustomDomain",
    summary="Update Custom Domain Routing",
)
@limiter.limit(Limits.DOMAIN_WRITE)
async def update_custom_domain(
    request: Request,
    domain_id: Annotated[str, Path(description="MongoDB ObjectId of the domain.")],
    body: UpdateCustomDomainRequest,
    service: CustomDomainSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes(DOMAIN_MANAGE_SCOPES)),  # noqa: B008
) -> CustomDomainResponse:
    """Update the per-domain routing config.

    Partial update — send only the fields you want to change. Omit a field to
    leave it untouched. Send explicit `null` to clear a stored value. The
    config takes effect only when the domain is `ACTIVE`; non-ACTIVE domains
    return 422.

    **Configurable fields**:
    - `root_redirect` — destination for `GET /` (302).
    - `not_found_redirect` — fallback for any path that doesn't match an alias.
    - `custom_robots_txt` — body served at `/robots.txt` (≤4096 chars).

    **Authentication**: Required (JWT or API key with `domains:manage`).

    **Rate Limits**: 30/min.

    **Responses**:
    - 200 — updated; full domain response returned
    - 403 — caller does not own this domain, or the plan lacks domain polish
    - 404 — domain not found (or invalid id)
    - 422 — domain isn't ACTIVE, or body fails validation
    """
    oid = _parse_domain_id(domain_id)
    # Setting any routing value needs the entitlement (the endpoint itself is
    # already behind custom_domains); clearing (null) never does.
    if not entitlements.has("domain_polish") and any(
        getattr(body, f) is not None
        for f in ("root_redirect", "not_found_redirect", "custom_robots_txt")
    ):
        raise PlanRequiredError("domain_polish")
    doc = await service.update_routing(oid, user, body)
    return CustomDomainResponse.from_doc(doc)


@router.delete(
    "/custom-domains/{domain_id}",
    responses=AUTH_RESPONSES,
    operation_id="deleteCustomDomain",
    summary="Revoke Custom Domain",
)
@limiter.limit(Limits.DOMAIN_DELETE)
async def delete_custom_domain(
    request: Request,
    domain_id: Annotated[str, Path(description="MongoDB ObjectId of the domain.")],
    service: CustomDomainSvc,
    cascade: bool = Query(
        default=False,
        description=(
            "When true, all URLs on this domain are deleted alongside the "
            "domain revoke. When false (default), URLs remain in the database "
            "but become unreachable (the domain stops resolving)."
        ),
    ),
    user: CurrentUser = Depends(require_scopes(DOMAIN_MANAGE_SCOPES)),  # noqa: B008
) -> CustomDomainDeleteResponse:
    """Revoke a custom domain. `REVOKED` is terminal.

    With `?cascade=true`, all URLs owned by the caller on the domain are
    bulk-deleted. With `?cascade=false` (default), URLs remain in the
    database and the domain stops serving — the URLs effectively become
    orphans until the domain is re-registered or garbage-collected.

    **Authentication**: Required (JWT or API key with `domains:manage`).

    **Rate Limits**: 10/min.
    """
    oid = _parse_domain_id(domain_id)
    doc, urls_deleted = await service.delete(oid, user, cascade=cascade)
    return CustomDomainDeleteResponse(
        id=str(doc.id),
        fqdn=doc.fqdn,
        cascade=cascade,
        urls_deleted=urls_deleted,
    )


@router.delete(
    "/custom-domains/{domain_id}/permanent",
    status_code=204,
    responses=AUTH_RESPONSES,
    operation_id="removeCustomDomain",
    summary="Remove Revoked Custom Domain",
)
@limiter.limit(Limits.DOMAIN_DELETE)
async def remove_custom_domain(
    request: Request,
    domain_id: Annotated[str, Path(description="MongoDB ObjectId of the domain.")],
    service: CustomDomainSvc,
    user: CurrentUser = Depends(require_scopes(DOMAIN_MANAGE_SCOPES)),  # noqa: B008
) -> None:
    """Permanently delete a revoked custom domain to free the account slot.

    Soft-deleted (REVOKED) domains continue to occupy the caller's per-account
    quota so revoke is reversible and the audit trail stays intact. To free
    the slot, the caller explicitly removes the doc via this endpoint. Only
    REVOKED docs are removable — call DELETE first to revoke an active one.

    **Authentication**: Required (JWT or API key with `domains:manage`).

    **Rate Limits**: 10/min.

    **Responses**:
    - 204 — removed
    - 403 — caller does not own this domain
    - 404 — domain not found (or invalid id)
    - 422 — domain isn't REVOKED
    """
    oid = _parse_domain_id(domain_id)
    await service.remove_revoked(oid, user)
    return None


__all__ = ["router"]
