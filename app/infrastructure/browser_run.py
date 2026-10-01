"""Cloudflare Browser Run client — the render egress.

One REST call per render: ``/snapshot`` returns the page's HTML and a
screenshot together. The render runs on Cloudflare's infrastructure, so
the destination sees a Cloudflare datacenter IP, never ours — the whole
point of the tier's egress rule. The caller is told which egress served
the render, because "the page looked clean" and "the page looked clean
to a scanner IP" are different pieces of evidence.

Failures return None rather than raising: a dead render is an absent
piece of evidence, not a broken investigation. But a render that failed
because we asked for too strict a wait is NOT absent evidence, it is a
wrong answer, so each wait condition is tried in turn before giving up.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.cloudflare.com/client/v4"
# networkidle0 wanted ZERO in-flight requests, so one analytics beacon kept
# a live page "loading" until it timed out. First success wins.
_WAIT_LADDER = ("networkidle2", "domcontentloaded")
EGRESS_LABEL = "cloudflare datacenter (Browser Run)"


@dataclass(frozen=True)
class RenderResult:
    url: str
    html: str
    screenshot: bytes  # webp
    egress: str = EGRESS_LABEL


def _is_wait_timeout(exc: Exception) -> bool:
    """Browser Run reports a goto timeout as error code 6002 in the body;
    a dead host is 5006 and never benefits from a looser wait."""
    response = getattr(exc, "response", None)
    text = getattr(response, "text", "") or ""
    return "6002" in text or "timeout was reached" in text.lower()


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
        self._last_was_wait_timeout = False

    @property
    def configured(self) -> bool:
        return bool(self._account_id and self._token)

    async def snapshot(self, url: str) -> RenderResult | None:
        """Render *url* and return HTML + screenshot, or None once every
        wait condition has failed (each logged). The browser follows
        JS/meta redirects the plain resolver cannot see — what it lands on
        is the truth of the page."""
        if not self.configured:
            log.warning("browser_run_unconfigured")
            return None
        for attempt, wait_until in enumerate(_WAIT_LADDER, start=1):
            result = await self._attempt(url, wait_until)
            if result is not None:
                if attempt > 1:
                    log.info("browser_run_recovered", url=url, wait_until=wait_until)
                return result
            if not self._last_was_wait_timeout:
                # A dead host fails the same way under every condition; only
                # a wait timeout is something the next rung can fix.
                return None
        return None

    async def _attempt(self, url: str, wait_until: str) -> RenderResult | None:
        endpoint = f"{_API_BASE}/accounts/{self._account_id}/browser-rendering/snapshot"
        try:
            response = await self._http.post(
                endpoint,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "url": url,
                    "screenshotOptions": {"type": "webp"},
                    "gotoOptions": {
                        "waitUntil": wait_until,
                        "timeout": int(self._timeout * 1000),
                    },
                },
                timeout=self._timeout + 15,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            self._last_was_wait_timeout = _is_wait_timeout(exc)
            log.warning(
                "browser_run_snapshot_failed",
                url=url,
                wait_until=wait_until,
                error=str(exc),
                error_type=type(exc).__name__,
                wait_timeout=self._last_was_wait_timeout,
            )
            return None
        self._last_was_wait_timeout = False
        result = body.get("result") or {}
        html = result.get("content") or ""
        shot_b64 = result.get("screenshot") or ""
        try:
            screenshot = base64.b64decode(shot_b64) if shot_b64 else b""
        except Exception:
            screenshot = b""
        if not html and not screenshot:
            log.warning("browser_run_empty_result", url=url, wait_until=wait_until)
            return None
        return RenderResult(url=url, html=html, screenshot=screenshot)
