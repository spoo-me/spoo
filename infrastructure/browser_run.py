"""Cloudflare Browser Run client — the render egress.

One REST call per render: ``/snapshot`` returns the page's HTML and a
screenshot together. The render runs on Cloudflare's infrastructure, so
the destination sees a Cloudflare datacenter IP, never ours — the whole
point of the tier's egress rule. The caller is told which egress served
the render, because "the page looked clean" and "the page looked clean
to a scanner IP" are different pieces of evidence.

Failures return None rather than raising: a dead render is an absent
piece of evidence, not a broken investigation.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.cloudflare.com/client/v4"
EGRESS_LABEL = "cloudflare datacenter (Browser Run)"


@dataclass(frozen=True)
class RenderResult:
    url: str
    html: str
    screenshot: bytes  # webp
    egress: str = EGRESS_LABEL


class BrowserRunClient:
    def __init__(
        self,
        http_client: HttpClient,
        *,
        account_id: str,
        api_token: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._http = http_client
        self._account_id = account_id
        self._token = api_token
        self._timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._account_id and self._token)

    async def snapshot(self, url: str) -> RenderResult | None:
        """Render *url* and return HTML + screenshot, or None on any
        failure (logged). The browser follows JS/meta redirects the plain
        resolver cannot see — what it lands on is the truth of the page."""
        if not self.configured:
            log.warning("browser_run_unconfigured")
            return None
        endpoint = f"{_API_BASE}/accounts/{self._account_id}/browser-rendering/snapshot"
        try:
            response = await self._http.post(
                endpoint,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "url": url,
                    "screenshotOptions": {"type": "webp"},
                    "gotoOptions": {
                        "waitUntil": "networkidle0",
                        "timeout": int(self._timeout * 1000),
                    },
                },
                timeout=self._timeout + 15,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            log.warning(
                "browser_run_snapshot_failed",
                url=url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        result = body.get("result") or {}
        html = result.get("content") or ""
        shot_b64 = result.get("screenshot") or ""
        try:
            screenshot = base64.b64decode(shot_b64) if shot_b64 else b""
        except Exception:
            screenshot = b""
        if not html and not screenshot:
            log.warning("browser_run_empty_result", url=url)
            return None
        return RenderResult(url=url, html=html, screenshot=screenshot)
