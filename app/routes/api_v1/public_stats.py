"""
GET|POST /api/v1/public/stats/{short_code} — public per-link statistics.

Serves the public stats page (/stats/{code}) for BOTH URL generations on
the system default domain. No auth required; an authenticated owner
session bypasses the privacy and password gates.

GET never accepts a password — not as a query param, not as a header
(an undeclared ``?password=`` is simply ignored). POST exists solely to
carry ``{"password"}`` in a JSON body; it accepts the same query params.
"""

from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import STATS_SCOPES, CurrentUser, optional_scopes
from app.dependencies.services import PublicStatsSvc
from app.middleware.openapi import ERROR_RESPONSES, OPTIONAL_AUTH_SECURITY
from app.middleware.rate_limiter import Limits, dynamic_limit, limiter
from app.schemas.dto.requests.public_stats import PublicStatsBody
from app.schemas.dto.responses.public_stats import PublicStatsResponse
from app.services.public_stats_service import PublicStatsService

router = APIRouter(tags=["Public"])

# Dedicated pair — NOT the generic API tier. Reasoning lives on
# Limits.PUBLIC_STATS_*: anon hits can run 90-day $facet aggregations,
# and this bucket doubles as the password-guess budget.
_public_stats_limit, _public_stats_key = dynamic_limit(
    Limits.PUBLIC_STATS_AUTHED, Limits.PUBLIC_STATS_ANON
)

_PATH = "/public/stats/{short_code}"

_START_DATE_QUERY = Query(
    default=None,
    max_length=50,
    description="Range start (ISO 8601). Defaults to 7 days before end_date.",
    examples=["2025-01-01T00:00:00Z"],
)
_END_DATE_QUERY = Query(
    default=None,
    max_length=50,
    description="Range end (ISO 8601). Defaults to now.",
    examples=["2025-12-31T23:59:59Z"],
)
_TIMEZONE_QUERY = Query(
    default="UTC",
    max_length=50,
    description="IANA timezone for time bucketing and formatting.",
    examples=["UTC", "America/New_York"],
)


async def _serve_public_stats(
    public_stats_service: PublicStatsService,
    short_code: str,
    *,
    start_date: str | None,
    end_date: str | None,
    timezone: str,
    password: str | None,
    user: CurrentUser | None,
) -> PublicStatsResponse:
    user_id: ObjectId | None = user.user_id if user is not None else None
    result = await public_stats_service.get_public_stats(
        short_code,
        start_date=start_date,
        end_date=end_date,
        tz_name=timezone,
        password=password,
        user_id=user_id,
    )
    return PublicStatsResponse.model_validate(result)


@router.get(
    _PATH,
    responses=ERROR_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="getPublicStats",
    summary="Public URL Statistics",
)
@limiter.limit(_public_stats_limit, key_func=_public_stats_key)
async def public_stats_get(
    request: Request,
    short_code: str,
    public_stats_service: PublicStatsSvc,
    start_date: str | None = _START_DATE_QUERY,
    end_date: str | None = _END_DATE_QUERY,
    timezone: str = _TIMEZONE_QUERY,
    user: CurrentUser | None = Depends(optional_scopes(STATS_SCOPES)),  # noqa: B008
) -> PublicStatsResponse:
    """Get public click statistics for a single short link.

    Resolves both URL generations (plus emoji aliases) on the system
    default domain and returns link facts plus the standard stats wire.

    **Authentication**: Optional — an owner session additionally sees
    private-stats links and skips the password gate.

    **Privacy**: A link with private stats answers exactly like a missing
    code (404, byte-identical). Password-protected links answer 401
    `password_required`; send the password via POST — never in the URL.

    **Rate Limits**:

    - Authenticated: 60/min, 2,000/day
    - Anonymous: 20/min, 500/day
    """
    return await _serve_public_stats(
        public_stats_service,
        short_code,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        password=None,
        user=user,
    )


@router.post(
    _PATH,
    responses=ERROR_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="getPublicStatsWithPassword",
    summary="Public URL Statistics (password unlock)",
)
@limiter.limit(_public_stats_limit, key_func=_public_stats_key)
async def public_stats_post(
    request: Request,
    short_code: str,
    public_stats_service: PublicStatsSvc,
    body: PublicStatsBody | None = None,
    start_date: str | None = _START_DATE_QUERY,
    end_date: str | None = _END_DATE_QUERY,
    timezone: str = _TIMEZONE_QUERY,
    user: CurrentUser | None = Depends(optional_scopes(STATS_SCOPES)),  # noqa: B008
) -> PublicStatsResponse:
    """Same as the GET variant, carrying a password in the JSON body.

    The body is the ONLY way a password travels to this endpoint —
    query-string passwords are ignored so they can't land in URLs, logs,
    or referrers. Wrong passwords answer 401 `invalid_password`
    (retryable). The body may be absent or empty.
    """
    return await _serve_public_stats(
        public_stats_service,
        short_code,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        password=body.password if body is not None else None,
        user=user,
    )
