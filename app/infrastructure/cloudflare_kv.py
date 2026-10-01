"""Async Cloudflare Workers KV client — edge-cache namespace only.

Scope: PUT/DELETE of edge-cache entries in a single KV namespace.
Custom Hostnames live in :mod:`infrastructure.cloudflare_client`; that
client's docstring scopes it deliberately, and the same rule applies
here in reverse. Transport (auth, retry, backoff) is shared via
:class:`CloudflareSession`.

Unlike ``CloudflareClient`` (which raises so wiring bugs surface), every
method here returns a bool: both call sites — the hot-URL promotion
action and future invalidation — are best-effort by contract, and a
raising client would just force try/except at each one. A failed PUT
means one URL isn't edge-served this window; a failed DELETE is bounded
by the KV entry's TTL. Both are logged, never fatal.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.infrastructure.cloudflare_client import CF_API_BASE
from app.infrastructure.cloudflare_session import CloudflareSession
from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

# CF's bulk write/delete endpoints accept at most 10,000 items per call.
_BULK_MAX_ITEMS = 10_000


class CloudflareKVClient:
    """Minimal Workers KV writer.

    The KV REST API is account-scoped:
    ``/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}``.
    ``api_base`` overrides the account-scoped root for local development —
    wrangler dev's Explorer API mirrors the same ``/storage/kv/...`` paths
    (``http://localhost:8787/cdn-cgi/explorer/api``).
    """

    def __init__(
        self,
        *,
        http_client: HttpClient,
        api_token: str | None,
        account_id: str | None,
        namespace_id: str | None,
        api_base: str | None = None,
        api_host_header: str | None = None,
        max_retries: int = 3,
        initial_backoff_seconds: float = 1.0,
    ) -> None:
        self._account_id = account_id
        self._namespace_id = namespace_id
        self._api_base = api_base
        self._token = api_token
        # base_url is meaningless while unconfigured; _request gates on
        # is_configured before the session is ever used.
        base_url = api_base or f"{CF_API_BASE}/accounts/{account_id}"
        self._session = CloudflareSession(
            http_client=http_client,
            api_token=api_token,
            base_url=base_url,
            host_header=api_host_header,
            max_retries=max_retries,
            initial_backoff_seconds=initial_backoff_seconds,
        )

    @property
    def is_configured(self) -> bool:
        return bool(
            self._token and self._namespace_id and (self._account_id or self._api_base)
        )

    async def put(
        self, key: str, value: str, *, expiration_ttl: int | None = None
    ) -> bool:
        """Write *value* under *key*.

        ``expiration_ttl`` auto-expires the entry after N seconds; ``None``
        persists it until an explicit delete (used by the meta-tags
        write-through, whose lifecycle is event-driven, not TTL-driven).
        """
        params = (
            {"expiration_ttl": expiration_ttl} if expiration_ttl is not None else None
        )
        return await self._request("PUT", key, content=value, params=params)

    async def delete(self, key: str) -> bool:
        """Idempotent delete — a 404 (already gone) counts as success."""
        return await self._request("DELETE", key, ok_statuses=(404,))

    async def bulk_put(
        self, pairs: list[tuple[str, str]], *, expiration_ttl: int | None = None
    ) -> bool:
        """Write many ``(key, value)`` pairs in one API call per 10k chunk.

        ``expiration_ttl`` applies to every entry (matching ``put``'s
        semantics); ``None`` persists until an explicit delete. Returns
        True only if every write succeeded.
        """
        if not pairs:
            return True
        ok = True
        for chunk in _chunked(pairs, _BULK_MAX_ITEMS):
            body = [
                {"key": key, "value": value}
                | (
                    {"expiration_ttl": expiration_ttl}
                    if expiration_ttl is not None
                    else {}
                )
                for key, value in chunk
            ]
            result = await self._bulk_request("PUT", "bulk", body, count=len(chunk))
            if result is None:
                return await self._put_one_by_one(pairs, expiration_ttl)
            ok = result and ok
        return ok

    async def bulk_delete(self, keys: list[str]) -> bool:
        """Delete many keys in one API call per 10k chunk.

        Like ``delete``, already-gone keys are not an error (the bulk
        endpoint doesn't 404 on missing keys). Returns True only if every
        delete succeeded.
        """
        if not keys:
            return True
        ok = True
        for chunk in _chunked(keys, _BULK_MAX_ITEMS):
            result = await self._bulk_request(
                "POST", "bulk/delete", list(chunk), count=len(chunk)
            )
            if result is None:
                return await self._delete_one_by_one(keys)
            ok = result and ok
        return ok

    async def _put_one_by_one(
        self, pairs: list[tuple[str, str]], expiration_ttl: int | None
    ) -> bool:
        ok = True
        for key, value in pairs:
            ok = await self.put(key, value, expiration_ttl=expiration_ttl) and ok
        return ok

    async def _delete_one_by_one(self, keys: list[str]) -> bool:
        ok = True
        for key in keys:
            ok = await self.delete(key) and ok
        return ok

    async def _bulk_request(
        self, method: str, subpath: str, body: list, *, count: int
    ) -> bool | None:
        """One bulk API call. ``None`` means the route itself is absent.

        Real CF never 404s these paths (missing keys are still 200), but
        wrangler dev's Explorer API mirrors only the single-key routes —
        callers degrade to per-key calls so the zero-cloud local loop
        exercises edge writes end-to-end instead of silently no-opping.
        """
        if not self.is_configured:
            log.warning("cf_kv_not_configured", method=method)
            return False
        path = f"/storage/kv/namespaces/{self._namespace_id}/{subpath}"
        try:
            response = await self._session.request(method, path, json=body)
        except httpx.HTTPError as exc:
            log.error(
                "cf_kv_retries_exhausted",
                method=method,
                path=subpath,
                count=count,
                error=str(exc),
            )
            return False
        if response.status_code == 404:
            log.info("cf_kv_bulk_unsupported", path=subpath, count=count)
            return None
        graded = self._grade_response(
            response, method=method, key=f"<{subpath}:{count}>"
        )
        if not graded:
            return False
        # CF's bulk endpoints can 200 while individual keys failed —
        # result.unsuccessful_keys lists them ("should be retried" per the
        # API docs). We don't retry (best-effort contract, TTL backstops)
        # but the return value must tell the truth.
        try:
            unsuccessful = (response.json().get("result") or {}).get(
                "unsuccessful_keys"
            ) or []
        except ValueError:
            unsuccessful = []  # non-JSON body (local emulator) — trust the 2xx
        if unsuccessful:
            log.error(
                "cf_kv_bulk_partial_failure",
                path=subpath,
                count=count,
                failed=len(unsuccessful),
                keys_preview=unsuccessful[:5],
            )
            return False
        return True

    async def _request(
        self,
        method: str,
        key: str,
        *,
        content: str | None = None,
        params: dict[str, int] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> bool:
        if not self.is_configured:
            log.warning("cf_kv_not_configured", method=method)
            return False

        # Keys ride in the URL path and can contain emoji (emoji short
        # codes) and reserved characters — CF requires them URL-encoded.
        # Encoding is transport-only: CF decodes before storing, so the
        # Worker's binding lookups see the raw key.
        path = (
            f"/storage/kv/namespaces/{self._namespace_id}/values/{quote(key, safe='')}"
        )
        try:
            response = await self._session.request(
                method, path, content=content, params=params
            )
        except httpx.HTTPError as exc:
            log.error(
                "cf_kv_retries_exhausted",
                method=method,
                key=key,
                error=str(exc),
            )
            return False

        return self._grade_response(
            response, method=method, key=key, ok_statuses=ok_statuses
        )

    def _grade_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        key: str,
        ok_statuses: tuple[int, ...] = (),
    ) -> bool:
        if response.is_success or response.status_code in ok_statuses:
            return True

        if response.status_code >= 500 or response.status_code == 429:
            # Session already retried and logged each attempt.
            log.error("cf_kv_retries_exhausted", method=method, key=key)
            return False

        # Other 4xx: config/auth problem, retry can't help — surface
        # loudly in logs and give up.
        log.error(
            "cf_kv_request_rejected",
            method=method,
            key=key,
            cf_status_code=response.status_code,
            cf_body_preview=response.text[:300],
        )
        return False


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
