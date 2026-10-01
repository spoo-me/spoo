"""Async Cloudflare API client — Custom Hostnames endpoints only.

Scope: SaaS Custom Hostnames CRUD (create/get/delete) for the spoo.me zone.
Anything broader belongs in a separate client. Bearer-token auth, tolerant
of missing config so self-hosters who skip CF entirely don't crash on
import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.errors import CloudflareAPIError, CloudflareNotConfiguredError
from app.infrastructure.cloudflare_session import CloudflareSession
from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

CF_API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass(frozen=True)
class CFHostnameResult:
    """Subset of CF Custom Hostname payload the service layer cares about."""

    id: str
    hostname: str
    status: str  # "pending" | "active" | "moved" | "deleted" | "blocked"
    ssl_status: str  # "initializing" | "pending_validation" | "active" | ...
    ssl_method: str = ""  # "http" | "txt" | "email"
    verification_errors: list[str] = field(default_factory=list)
    # DCV records the customer needs to add. Shape depends on dcv_method:
    # - delegated: empty (handled via permanent CNAME)
    # - http: [{name: "<token>", value: "<expected>", type: "http"}]
    # - txt:  [{name: "_acme-challenge.<host>", value: "<token>", type: "txt"}]
    verification_records: list[dict[str, Any]] = field(default_factory=list)
    # Pre-validation TXT (zone setting). Populated when CF requires the
    # customer to prove ownership before DCV runs. None when the zone
    # has pre-validation disabled.
    ownership_verification: dict[str, str] | None = None


class CloudflareClient:
    """Minimal CF Custom Hostnames client with retry on 5xx.

    All methods raise ``CloudflareNotConfiguredError`` when the bearer token
    or zone id is missing — wiring should never construct this client without
    them, so a raise here surfaces a wiring bug, not a customer-visible error.
    """

    def __init__(
        self,
        *,
        http_client: HttpClient,
        api_token: str | None,
        zone_id: str | None,
        max_retries: int = 3,
        initial_backoff_seconds: float = 1.0,
    ) -> None:
        self._token = api_token
        self._zone_id = zone_id
        self._max_retries = max_retries
        self._session = CloudflareSession(
            http_client=http_client,
            api_token=api_token,
            base_url=CF_API_BASE,
            max_retries=max_retries,
            initial_backoff_seconds=initial_backoff_seconds,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self._token and self._zone_id)

    async def create_custom_hostname(
        self,
        fqdn: str,
        *,
        dcv_method: str,
        custom_origin_server: str | None = None,
    ) -> CFHostnameResult:
        """Register *fqdn* with CF SaaS.

        ``dcv_method`` is one of ``"txt"`` (delegated), ``"http"``, ``"email"``.
        ``custom_origin_server`` overrides the zone fallback origin per
        hostname — CF dispatches request to this hostname. Required when a
        Worker route on the SaaS zone needs to catch dispatched traffic.
        """
        body: dict = {
            "hostname": fqdn,
            "ssl": {
                "method": dcv_method,
                "type": "dv",
                "settings": {"min_tls_version": "1.2"},
            },
        }
        if custom_origin_server:
            body["custom_origin_server"] = custom_origin_server
        payload = await self._request(
            "POST",
            f"/zones/{self._zone_id}/custom_hostnames",
            json=body,
        )
        return _parse_hostname(payload["result"])

    async def recheck_custom_hostname(
        self, hostname_id: str, *, dcv_method: str
    ) -> CFHostnameResult:
        """Trigger an immediate CF DCV re-probe.

        PATCH with the same ssl.method pokes CF to re-validate now instead
        of waiting for its next scheduled retry. Used on user-initiated
        verify clicks so they don't sit in CF's 15+ minute backoff window.
        """
        payload = await self._request(
            "PATCH",
            f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}",
            json={"ssl": {"method": dcv_method, "type": "dv"}},
        )
        return _parse_hostname(payload["result"])

    async def get_custom_hostname(self, hostname_id: str) -> CFHostnameResult:
        payload = await self._request(
            "GET",
            f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}",
        )
        return _parse_hostname(payload["result"])

    async def delete_custom_hostname(self, hostname_id: str) -> bool:
        """Idempotent — 404 returns True (already gone)."""
        try:
            await self._request(
                "DELETE",
                f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}",
            )
        except CloudflareAPIError as exc:
            if exc.details and exc.details.get("status_code") == 404:
                return True
            raise
        return True

    async def find_hostname_by_fqdn(self, fqdn: str) -> CFHostnameResult | None:
        """Recovery path when a doc lost its ``cf_hostname_id``."""
        payload = await self._request(
            "GET",
            f"/zones/{self._zone_id}/custom_hostnames",
            params={"hostname": fqdn},
        )
        results = payload.get("result") or []
        if not results:
            return None
        return _parse_hostname(results[0])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise CloudflareNotConfiguredError(
                "CloudflareClient invoked without api_token + zone_id."
            )

        # Transport (auth, retry on transport errors + 5xx/429 with
        # Retry-After) lives in the session; everything below interprets
        # the final response.
        #
        # Raw bodies + CF's `errors`/`messages` go to logs only;
        # `CloudflareAPIError.to_dict()` strips `details` entirely from
        # the API response so zone IDs / internal metadata can't leak.
        # The status code stays on `details` because internal callers
        # use it for control flow (e.g. 404 = idempotent delete).
        try:
            response = await self._session.request(
                method, path, json=json, params=params
            )
        except httpx.HTTPError as exc:
            log.error(
                "cf_api_retries_exhausted",
                method=method,
                path=path,
                attempts=self._max_retries,
                underlying_error=str(exc),
            )
            raise CloudflareAPIError(
                f"CF API {method} {path} failed after {self._max_retries} attempts",
            ) from exc

        if response.status_code >= 500 or response.status_code == 429:
            # Session already retried and logged each attempt.
            raise CloudflareAPIError(
                f"CF API {method} {path} returned {response.status_code}",
                details={"status_code": response.status_code},
            )

        if not response.is_success:
            log.error(
                "cf_api_error",
                method=method,
                path=path,
                cf_status_code=response.status_code,
                cf_body_preview=response.text[:500],
            )
            raise CloudflareAPIError(
                f"CF API {method} {path} returned {response.status_code}",
                details={"status_code": response.status_code},
            )

        # CF v4 can return 2xx with `success: false` for validation
        # failures — must surface as our error type, not a downstream
        # KeyError on `result`.
        try:
            payload = response.json()
        except ValueError as exc:
            log.error(
                "cf_api_malformed_json",
                method=method,
                path=path,
                cf_status_code=response.status_code,
                cf_body_preview=response.text[:500],
            )
            raise CloudflareAPIError(
                f"CF API {method} {path} returned malformed JSON",
                details={"status_code": response.status_code},
            ) from exc

        if isinstance(payload, dict) and payload.get("success") is False:
            log.error(
                "cf_api_success_false",
                method=method,
                path=path,
                cf_status_code=response.status_code,
                cf_errors=payload.get("errors"),
                cf_messages=payload.get("messages"),
            )
            raise CloudflareAPIError(
                f"CF API {method} {path} reported success=false",
                details={"status_code": response.status_code},
            )

        return payload


def _parse_hostname(raw: dict[str, Any]) -> CFHostnameResult:
    ssl = raw.get("ssl") or {}
    ov_raw = raw.get("ownership_verification") or None
    ownership = None
    if isinstance(ov_raw, dict) and ov_raw.get("name") and ov_raw.get("value"):
        ownership = {
            "type": ov_raw.get("type", "txt"),
            "name": str(ov_raw["name"]),
            "value": str(ov_raw["value"]),
        }
    return CFHostnameResult(
        id=raw["id"],
        hostname=raw["hostname"],
        status=raw.get("status", "unknown"),
        ssl_status=ssl.get("status", "unknown"),
        ssl_method=ssl.get("method", ""),
        verification_errors=list(ssl.get("validation_errors") or []),
        verification_records=list(ssl.get("validation_records") or []),
        ownership_verification=ownership,
    )
