"""GET /api/v1/metadata — fetch & parse a destination's existing meta tags.

Two consumers, one route: the custom meta-tags editor prefills
title/description/image from here before customizing, and the public
link preview checker at spoo.me/tools/link-preview shows how a page
unfurls. Auth is optional — the fetch-proxy
concern is held by rate limits, not login: authenticated callers spend a
per-account budget, anonymous callers a tighter per-IP one. Fetching
is SSRF-guarded and Redis-cached (1h / 5m negative), and the response
exposes only parsed tags, never page content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from dependencies import (
    URL_READ_SCOPES,
    CurrentUser,
    Settings,
    optional_scopes,
)
from errors import AppError, ValidationError
from infrastructure.logging import get_logger
from infrastructure.safe_fetch import (
    FetchHardError,
    FetchTransientError,
    fetch_public,
)
from middleware.openapi import ERROR_RESPONSES, OPTIONAL_AUTH_SECURITY
from middleware.rate_limiter import Limits, dynamic_limit, limiter
from schemas.dto.responses.metadata import MetadataResponse
from services.meta_tags.parse_html import parse_meta_tags

log = get_logger(__name__)

router = APIRouter(tags=["Metadata"])

_FETCH_MAX_BYTES = 524_288  # tags must be in the head; 512KB is generous
_FETCH_TIMEOUT = 5.0

_metadata_limit, _metadata_key = dynamic_limit(
    Limits.METADATA_FETCH, Limits.METADATA_ANON
)


class UpstreamUnfetchableError(AppError):
    """The destination can't be fetched or isn't an HTML page."""

    status_code = 422
    error_code = "unfetchable"


class UpstreamTimeoutError(AppError):
    status_code = 504
    error_code = "upstream_timeout"


@router.get(
    "/metadata",
    responses=ERROR_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="getUrlMetadata",
    summary="Fetch Destination Meta Tags",
)
@limiter.limit(_metadata_limit, key_func=_metadata_key)
async def get_metadata(
    request: Request,
    url: Annotated[
        str,
        Query(
            max_length=2048,
            description="Destination https URL to fetch and parse.",
            examples=["https://example.com/article"],
        ),
    ],
    settings: Settings,
    user: CurrentUser | None = Depends(optional_scopes(URL_READ_SCOPES)),  # noqa: B008
) -> MetadataResponse:
    """Fetch a destination page and return its existing meta tags.

    Use this to prefill ``meta_tags`` before customizing a link's social
    preview, or to check how a page will unfurl. Returns normalized
    best-pick fields (og → twitter → html fallbacks) plus the raw
    ``og``/``twitter`` tag families.

    **Authentication**: Optional. Authenticated callers get the higher
    per-account limit; anonymous calls are limited per IP. **API Key
    Scope** (when authenticating): `urls:read`, `urls:manage`, or
    `admin:all`.

    **Rate Limits**: 60/min, 2,000/day authenticated; 15/min, 300/day
    anonymous — results are cached ~1h server-side, so repeat calls for
    the same URL are cheap and don't refetch.
    """
    if not url.startswith("https://"):
        raise ValidationError("url must be https", field="url")

    cache = request.app.state.meta_fetch_cache
    cached = await cache.get(url)
    if cached is not None:
        if cached.get("error"):
            raise UpstreamUnfetchableError(cached["error"])
        return MetadataResponse(**cached)

    try:
        fetched = await fetch_public(
            url,
            accept_content=("text/html", "application/xhtml"),
            timeout=_FETCH_TIMEOUT,
            max_bytes=_FETCH_MAX_BYTES,
            # Big pages are fine — tags live in <head>, parse the prefix.
            truncate_over_cap=True,
            user_agent=settings.meta_tags.fetch_user_agent,
        )
    except FetchTransientError as exc:
        raise UpstreamTimeoutError("destination did not respond in time") from exc
    except FetchHardError as exc:
        # Generic message only — the specific reason would let a caller probe
        # whether hostnames resolve / point at private space. Detail to logs.
        log.info("metadata_fetch_unfetchable", url=url, reason=str(exc))
        payload = {"error": "destination is not a fetchable HTML page"}
        await cache.set(url, payload, negative=True)
        raise UpstreamUnfetchableError(payload["error"]) from exc

    parsed = parse_meta_tags(
        fetched.data.decode("utf-8", errors="replace"), fetched.final_url
    )
    payload = {
        "url": url,
        "final_url": fetched.final_url,
        "title": parsed.title,
        "description": parsed.description,
        "image": parsed.image,
        "color": parsed.color,
        "site_name": parsed.site_name,
        "html_title": parsed.html_title,
        "html_description": parsed.html_description,
        "favicon": parsed.favicon,
        "og": parsed.og,
        "twitter": parsed.twitter,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await cache.set(url, payload)
    return MetadataResponse(**payload)
