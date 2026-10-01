"""Unit tests for HttpPostHogEraser."""

from unittest.mock import AsyncMock, MagicMock

from app.infrastructure.posthog_erasure import HttpPostHogEraser


class TestHttpPostHogEraser:
    def _make(self, host="https://eu.posthog.com"):
        http = MagicMock()
        http.post = AsyncMock(return_value=MagicMock(status_code=200))
        eraser = HttpPostHogEraser(
            http, api_key="phx_secret", project_id="12345", host=host
        )
        return eraser, http

    async def test_happy_path_posts_bulk_delete(self):
        eraser, http = self._make()
        await eraser.delete_person("64f0c0ffee")
        args, kwargs = http.post.call_args
        assert args[0] == (
            "https://eu.posthog.com/api/projects/12345/persons/bulk_delete/"
        )
        assert kwargs["json"] == {
            "distinct_ids": ["64f0c0ffee"],
            "delete_events": True,
        }
        assert kwargs["headers"]["Authorization"] == "Bearer phx_secret"

    async def test_trailing_slash_host_normalized(self):
        eraser, http = self._make(host="https://eu.posthog.com/")
        await eraser.delete_person("abc")
        args, _ = http.post.call_args
        assert args[0].startswith("https://eu.posthog.com/api/")

    async def test_non_2xx_swallowed(self):
        eraser, http = self._make()
        http.post = AsyncMock(return_value=MagicMock(status_code=403, text="denied"))
        # Must not raise — the erasure cascade treats PostHog as best-effort.
        await eraser.delete_person("abc")

    async def test_network_error_swallowed(self):
        eraser, http = self._make()
        http.post = AsyncMock(side_effect=Exception("connect timeout"))
        await eraser.delete_person("abc")
