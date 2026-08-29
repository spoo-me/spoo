"""Terminal-URL resolution for redirector destinations.

Every hop resolves through the shared safe_fetch guard and connects to
the pinned IP; JS and meta-refresh redirects are invisible by design.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlparse

import httpx

from infrastructure.logging import get_logger
from infrastructure.safe_fetch import (
    FetchHardError,
    FetchTransientError,
    bracket_ip,
    resolve_public_ip,
)

log = get_logger(__name__)

_MAX_HOPS = 6
_HOP_TIMEOUT = 5.0
_TOTAL_TIMEOUT = 12.0


async def resolve_terminal_url(url: str) -> str | None:
    """Follow HTTP redirects to the final URL; None when the chain cannot
    be walked (unsupported scheme, private address, network failure, hop
    ceiling). None means "unresolved", never "clean"."""

    async def _walk() -> str | None:
        current = url
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=_HOP_TIMEOUT
        ) as client:
            for _hop in range(_MAX_HOPS):
                parsed = urlparse(current)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    return None
                try:
                    ip = await resolve_public_ip(parsed.hostname)
                except (FetchHardError, FetchTransientError):
                    return None
                pinned = httpx.URL(current).copy_with(host=bracket_ip(ip))
                headers = {"Host": parsed.hostname}
                ext = {"sni_hostname": parsed.hostname}
                try:
                    response = await client.head(
                        pinned, headers=headers, extensions=ext
                    )
                    if response.status_code in (405, 501):
                        req = client.build_request(
                            "GET", pinned, headers=headers, extensions=ext
                        )
                        response = await client.send(req, stream=True)
                        await response.aclose()
                except httpx.HTTPError:
                    return None
                location = response.headers.get("location")
                if response.is_redirect and location:
                    current = urljoin(current, location)
                    continue
                return current
        return None

    # wait_for, not asyncio.timeout: 3.10 support (see safe_fetch).
    try:
        return await asyncio.wait_for(_walk(), timeout=_TOTAL_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        return None
