"""Google Web Risk Lookup API (``uris:search``).

One credential and one implementation behind both callers: the safety
pipeline's analyzer provider and the URL expander tool. The Safe Browsing
API is non-commercial-only; Web Risk is the sanctioned equivalent.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

# Enforcement drives auto-blocking, so widening it is a policy change and
# never a side effect of touching the display list.
ENFORCEMENT_THREAT_TYPES = ("MALWARE", "SOCIAL_ENGINEERING")
DISPLAY_THREAT_TYPES = (*ENFORCEMENT_THREAT_TYPES, "UNWANTED_SOFTWARE")


class WebRiskClient:
    """Judges a full URL against Google's threat lists.

    ``lookup`` returns the matched threat types, an empty list when the URL
    is clean, and None when the lookup could not answer. Callers must treat
    None as absence, never as a clean verdict. A match Google declines to
    categorise still counts, as ``["UNKNOWN"]``.
    """

    def __init__(
        self,
        http_client: HttpClient,
        *,
        api_key: str,
        api_base: str = "https://webrisk.googleapis.com",
        threat_types: Sequence[str],
        timeout: float = 10.0,
    ) -> None:
        self._http = http_client
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._threat_types = list(threat_types)
        self._timeout = timeout

    async def lookup(self, url: str) -> list[str] | None:
        try:
            # Key rides a header: httpx logs full request URLs at INFO.
            response = await self._http.get(
                f"{self._api_base}/v1/uris:search",
                params={"uri": url, "threatTypes": self._threat_types},
                headers={"X-Goog-Api-Key": self._api_key},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                log.warning(
                    "web_risk_lookup_failed", error=f"http {response.status_code}"
                )
                return None
            threat = response.json().get("threat")
            if not threat:
                return []
            return threat.get("threatTypes") or ["UNKNOWN"]
        # Broad on purpose: every caller treats an unanswered lookup as
        # absence, so no failure here may propagate.
        except Exception as exc:
            log.warning("web_risk_lookup_failed", error=type(exc).__name__)
            return None
