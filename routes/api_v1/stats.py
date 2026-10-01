"""
GET /api/v1/stats               — URL click statistics (account aggregate).
GET /api/v1/stats/links/{id}    — click statistics for one owned URL.

Both require authentication — every stats read is scoped to the caller's
own URLs. Public per-link stats live at GET /api/v1/public/stats/{code}.
API key users require ``stats:read``, ``urls:read``, or ``admin:all``.

Route-ordering note: the two paths differ in segment count, so neither can
shadow the other. ``links`` is a typed segment — future siblings
(``/stats/domains/{fqdn}``, ``/stats/groups/{id}``) can land without
route-shadowing games.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from dependencies import (
    STATS_SCOPES,
    CurrentUser,
    Entitled,
    StatsSvc,
    UrlSvc,
    require_scopes,
)
from middleware.openapi import ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter, plan_scaled
from routes.api_v1._helpers import parse_url_id
from schemas.dto.requests.stats import LinkStatsQuery, StatsQuery
from schemas.dto.responses.stats import LinkStatsResponse, StatsResponse

router = APIRouter(tags=["Statistics"])


@router.get(
    "/stats",
    responses=ERROR_RESPONSES,
    operation_id="getStats",
    summary="URL Statistics",
)
@limiter.limit(plan_scaled(Limits.API_AUTHED))
async def stats_v1(
    request: Request,
    entitlements: Entitled,
    query: Annotated[StatsQuery, Query()],
    stats_service: StatsSvc,
    user: CurrentUser = Depends(require_scopes(STATS_SCOPES)),  # noqa: B008
) -> StatsResponse:
    """Get aggregated click statistics across all URLs you own.

    Retrieve click analytics with flexible grouping, filtering, and
    time-range options.

    **Authentication**: Required.

    **API Key Scope**: `stats:read`, `urls:read`, or `admin:all`

    **Rate Limits**: 60/min, 5,000/day

    **Grouping Dimensions**: `time`, `browser`, `os`, `device`, `country`,
    `city`, `referrer`, `short_code`, `utm_source`, `utm_medium`,
    `utm_campaign`, `variant`

    **Metrics**: `clicks`, `unique_clicks`

    **Filtering**: Filter by `browser`, `os`, `device`, `country`, `city`,
    `referrer`, `short_code`, `url_id`, `variant`, or the `utm_*` tags using
    query params or a JSON `filters` object. Filters slice your own aggregate —
    `url_id` values you do not own simply match nothing. For statistics on
    a single link, prefer `GET /stats/links/{url_id}`.
    """
    result = await stats_service.query(
        query,
        str(user.user_id),
        window_days=entitlements.limit("analytics_window_days"),
    )
    return StatsResponse.model_validate(result)


@router.get(
    "/stats/links/{url_id}",
    responses=ERROR_RESPONSES,
    operation_id="getLinkStats",
    summary="Link Statistics",
)
@limiter.limit(plan_scaled(Limits.API_AUTHED))
async def link_stats_v1(
    request: Request,
    entitlements: Entitled,
    url_id: Annotated[
        str,
        Path(description="Unique identifier of the URL (MongoDB ObjectId)."),
    ],
    query: Annotated[LinkStatsQuery, Query()],
    stats_service: StatsSvc,
    url_service: UrlSvc,
    user: CurrentUser = Depends(require_scopes(STATS_SCOPES)),  # noqa: B008
) -> LinkStatsResponse:
    """Get click statistics for a single URL you own.

    The same aggregated analytics as `GET /stats`, pre-scoped to one link —
    the response additionally echoes the link's `url_id` and `alias`.
    Custom-domain links are safe here: clicks are matched by URL id, so a
    same-alias link on another domain can never bleed in.

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `stats:read`, `urls:read`, or `admin:all`

    **Rate Limits**: 60/min, 5,000/day

    **Grouping Dimensions**: `time`, `browser`, `os`, `device`, `country`,
    `city`, `referrer`, `utm_source`, `utm_medium`, `utm_campaign`, `variant`

    **Metrics**: `clicks`, `unique_clicks`

    **Filtering**: Filter by `browser`, `os`, `device`, `country`, `city`,
    `referrer`, `variant`, or the `utm_*` tags using query params or a JSON
    `filters` object. Link-identity filters (`short_code`, `url_id`) do not exist
    here — the path already selects the link.

    **Errors**:

    - `400` — malformed id (not a valid ObjectId)
    - `404` — no URL with that id in your account. A URL owned by someone
      else answers identically; this endpoint never confirms foreign ids.
    """
    oid = parse_url_id(url_id)
    url = await url_service.get_owned(oid, user.user_id)
    result = await stats_service.query_link(
        query, url, window_days=entitlements.limit("analytics_window_days")
    )
    return LinkStatsResponse.model_validate(result)
