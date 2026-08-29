"""GET /api/v1/expand — follow a short link's redirect chain.

Backs the URL expander at spoo.me/tools/url-expander. Every hop is
SSRF-guarded exactly like the metadata fetch; unlike it, plain-http hops
are allowed (only headers ever ride the wire) and reported so the UI can
flag them. The only safety claim made is the one we can compute: whether
any hop matches the abuse blocklist enforced at link creation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from dependencies import UrlExpandSvc
from errors import AppError, ValidationError
from infrastructure.logging import get_logger
from infrastructure.safe_fetch import FetchHardError, FetchTransientError
from middleware.openapi import ERROR_RESPONSES, OPTIONAL_AUTH_SECURITY
from middleware.rate_limiter import Limits, dynamic_limit, limiter
from schemas.dto.responses.expand import ExpandResponse

log = get_logger(__name__)

router = APIRouter(tags=["Metadata"])

_expand_limit, _expand_key = dynamic_limit(Limits.METADATA_FETCH, Limits.METADATA_ANON)


class ChainUnfetchableError(AppError):
    """The first hop can't be reached at all."""

    status_code = 422
    error_code = "unfetchable"


class ChainTimeoutError(AppError):
    status_code = 504
    error_code = "upstream_timeout"


@router.get(
    "/expand",
    responses=ERROR_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="expandUrl",
    summary="Expand a Short Link",
)
@limiter.limit(_expand_limit, key_func=_expand_key)
async def expand_url(
    request: Request,
    url: Annotated[
        str,
        Query(
            max_length=2048,
            description="Short or redirecting URL to follow, http or https.",
            examples=["https://spoo.me/example"],
        ),
    ],
    expand_service: UrlExpandSvc,
) -> ExpandResponse:
    """Follow a URL's redirect chain and return every hop in order.

    Works on links from any shortener. Bodies are never fetched — only
    each hop's status and Location header — and results are cached ~1h
    server-side, so repeat calls for the same URL don't refetch.

    **Authentication**: None required. Authenticated callers get the
    higher per-account rate limit; anonymous calls are limited per IP.

    **Rate Limits**: 60/min, 2,000/day authenticated; 15/min, 300/day
    anonymous.
    """
    if not url.startswith(("https://", "http://")):
        raise ValidationError("url must be http(s)", field="url")

    try:
        payload = await expand_service.expand(url)
    except FetchTransientError as exc:
        raise ChainTimeoutError("destination did not respond in time") from exc
    except FetchHardError as exc:
        # Generic message only — mirrors GET /metadata: the specific reason
        # would let a caller probe private space. Detail to logs.
        log.info("expand_unfetchable", url=url, reason=str(exc))
        raise ChainUnfetchableError("that URL can't be expanded") from exc
    return ExpandResponse(**payload)
