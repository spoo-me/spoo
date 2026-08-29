"""GET /api/v1/domain-intel — public records of a destination host.

Backs the URL expander tool's records panel: DNS, RDAP registration
data, and the TLS certificate. Facts only, stated as fetched; the tool
links out to reputation checkers rather than repeating their verdicts.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Query, Request

from dependencies import DomainIntelSvc
from errors import AppError, ValidationError
from infrastructure.logging import get_logger
from infrastructure.safe_fetch import FetchHardError, FetchTransientError
from middleware.openapi import ERROR_RESPONSES, OPTIONAL_AUTH_SECURITY
from middleware.rate_limiter import Limits, dynamic_limit, limiter
from schemas.dto.responses.domain_intel import DomainIntelResponse

log = get_logger(__name__)

router = APIRouter(tags=["Metadata"])

_intel_limit, _intel_key = dynamic_limit(Limits.METADATA_FETCH, Limits.METADATA_ANON)

_HOSTNAME_RE = re.compile(
    r"^(?=.{4,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"
)


class HostUnresolvableError(AppError):
    status_code = 422
    error_code = "unfetchable"


class LookupTimeoutError(AppError):
    status_code = 504
    error_code = "upstream_timeout"


@router.get(
    "/domain-intel",
    responses=ERROR_RESPONSES,
    openapi_extra=OPTIONAL_AUTH_SECURITY,
    operation_id="getDomainIntel",
    summary="Domain Records",
)
@limiter.limit(_intel_limit, key_func=_intel_key)
async def get_domain_intel(
    request: Request,
    host: Annotated[
        str,
        Query(
            max_length=253,
            description="Hostname to look up (no scheme or path).",
            examples=["github.com"],
        ),
    ],
    intel_service: DomainIntelSvc,
) -> DomainIntelResponse:
    """DNS records, registration data, and TLS certificate of a host.

    Registration comes from the registry's own RDAP server; ``age_days``
    is the strongest quick signal (freshly registered domains are the
    phishing tell). Results are cached ~24h server-side.

    **Authentication**: None required. Authenticated callers get the
    higher per-account rate limit; anonymous calls are limited per IP.

    **Rate Limits**: 60/min, 2,000/day authenticated; 15/min, 300/day
    anonymous.
    """
    hostname = host.strip().lower().rstrip(".")
    if not _HOSTNAME_RE.match(hostname):
        raise ValidationError("not a valid hostname", field="host")

    try:
        payload = await intel_service.lookup(hostname)
    except FetchTransientError as exc:
        raise LookupTimeoutError("lookups did not respond in time") from exc
    except FetchHardError as exc:
        log.info("domain_intel_unresolvable", host=hostname, reason=str(exc))
        raise HostUnresolvableError("that host does not resolve") from exc
    return DomainIntelResponse(**payload)
