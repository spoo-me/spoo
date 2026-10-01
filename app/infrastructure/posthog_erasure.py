"""PostHog person deletion for the account-erasure cascade.

HTTP implementation of the ``PostHogEraser`` protocol
(``services/account_erasure_service.py``). Uses PostHog's
bulk-delete-by-distinct-id API, which removes the person AND their
events — the analytics half of a GDPR Art. 17 erasure.

Strictly best-effort: any failure (non-2xx, network error) is logged as
a warning and swallowed. The cascade must never park an account in
PENDING_DELETION because the analytics vendor is down; the step is
idempotent, so a retried sweep repeats it for free.
"""

from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class HttpPostHogEraser:
    def __init__(
        self,
        http_client: HttpClient,
        *,
        api_key: str,
        project_id: str,
        host: str = "https://eu.posthog.com",
    ) -> None:
        self._http = http_client
        self._api_key = api_key
        self._project_id = project_id
        self._host = host.rstrip("/")

    async def delete_person(self, distinct_id: str) -> None:
        url = f"{self._host}/api/projects/{self._project_id}/persons/bulk_delete/"
        payload = {"distinct_ids": [distinct_id], "delete_events": True}
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            response = await self._http.post(url, json=payload, headers=headers)
        except Exception as e:
            log.warning(
                "posthog_person_delete_error",
                distinct_id=distinct_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return

        if 200 <= response.status_code < 300:
            log.info("posthog_person_deleted", distinct_id=distinct_id)
            return
        log.warning(
            "posthog_person_delete_failed",
            distinct_id=distinct_id,
            status_code=response.status_code,
            response=response.text[:200],
        )
