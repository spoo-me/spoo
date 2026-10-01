"""Shared transport for Cloudflare REST calls.

Owns what every CF client would otherwise duplicate (and let drift):
bearer auth, base-URL joining, and the retry loop — transport errors and
5xx/429 responses retry with exponential backoff, 429 honouring
``Retry-After``. Response SEMANTICS stay with the callers:
``CloudflareClient`` raises typed errors, ``CloudflareKVClient`` returns
bools. This class never interprets a status code beyond retryability.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class CloudflareSession:
    def __init__(
        self,
        *,
        http_client: HttpClient,
        api_token: str | None,
        base_url: str,
        host_header: str | None = None,
        max_retries: int = 3,
        initial_backoff_seconds: float = 1.0,
    ) -> None:
        self._http = http_client
        self._token = api_token
        self._base_url = base_url
        # Dev-only: wrangler dev's Explorer API validates the Host header
        # (allows localhost forms only), so a container reaching it via
        # host.docker.internal must present "localhost:8787". Never set
        # against the real Cloudflare API.
        self._host_header = host_header
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff_seconds

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        content: str | None = None,
    ) -> httpx.Response:
        """Perform *method* on ``base_url + path`` with retries.

        Returns the final response — including a 5xx/429 one after
        retries are exhausted (callers decide whether that raises or
        degrades). Raises ``httpx.HTTPError`` only when every attempt
        failed at the transport layer.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._host_header:
            headers["Host"] = self._host_header
        url = f"{self._base_url}{path}"

        last_exc: httpx.HTTPError | None = None
        last_response: httpx.Response | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self._http.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                    content=content,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning(
                    "cf_api_transport_error",
                    method=method,
                    path=path,
                    error=str(exc),
                    attempt=attempt,
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 500 or response.status_code == 429:
                log.warning(
                    "cf_api_retryable_error",
                    method=method,
                    path=path,
                    cf_status_code=response.status_code,
                    cf_body_preview=response.text[:500],
                    attempt=attempt,
                )
                last_response = response
                await self._sleep_backoff(
                    attempt, override_seconds=_parse_retry_after(response)
                )
                continue

            return response

        if last_response is not None:
            return last_response
        assert last_exc is not None  # loop ran: one of the two is set
        raise last_exc

    async def _sleep_backoff(
        self, attempt: int, *, override_seconds: float | None = None
    ) -> None:
        # Last attempt — let the caller handle the outcome instead of
        # sleeping pointlessly.
        if attempt >= self._max_retries - 1:
            return
        delay = (
            override_seconds
            if override_seconds is not None
            else self._initial_backoff * (2**attempt)
        )
        await asyncio.sleep(delay)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Honour Retry-After (seconds form); malformed input falls back to
    exponential backoff silently."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (ValueError, TypeError):
        return None
