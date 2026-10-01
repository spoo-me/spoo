"""Unit tests for HttpClient."""

from unittest.mock import MagicMock

import pytest

from app.infrastructure.http_client import HttpClient


class TestHttpClient:
    async def test_post_delegates_to_httpx(self, mocker):
        client = HttpClient()
        fake_resp = MagicMock(status_code=200)
        mocker.patch.object(client._client, "post", return_value=fake_resp)
        resp = await client.post("http://example.com")
        assert resp.status_code == 200
        await client.aclose()

    async def test_get_delegates_to_httpx(self, mocker):
        client = HttpClient()
        fake_resp = MagicMock(status_code=200)
        mocker.patch.object(client._client, "get", return_value=fake_resp)
        resp = await client.get("http://example.com")
        assert resp.status_code == 200
        await client.aclose()

    async def test_post_propagates_exception(self, mocker):
        client = HttpClient()
        mocker.patch.object(client._client, "post", side_effect=Exception("timeout"))
        with pytest.raises(Exception, match="timeout"):
            await client.post("http://example.com")
        await client.aclose()

    async def test_context_manager(self):
        async with HttpClient() as client:
            assert client is not None
