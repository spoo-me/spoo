"""Unit tests for CloudflareKVClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from app.infrastructure.cloudflare_kv import CloudflareKVClient


def _http_with_response(
    status_code: int = 200, json_body: dict | None = None
) -> tuple[MagicMock, AsyncMock]:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.text = "body"
    response.headers = {}
    response.json = MagicMock(
        return_value={"success": True} if json_body is None else json_body
    )
    http = MagicMock()
    http.request = AsyncMock(return_value=response)
    return http, http.request


def _client(http, **overrides) -> CloudflareKVClient:
    kwargs = {
        "http_client": http,
        "api_token": "tok",
        "account_id": "acc",
        "namespace_id": "ns",
        "max_retries": 2,
        "initial_backoff_seconds": 0.001,
    }
    kwargs.update(overrides)
    return CloudflareKVClient(**kwargs)


class TestConfigurationGate:
    def test_configured_when_all_three_set(self):
        http, _ = _http_with_response()
        assert _client(http).is_configured is True

    async def test_unconfigured_returns_false_without_http_call(self):
        http, request = _http_with_response()
        client = _client(http, api_token=None)
        assert client.is_configured is False
        assert await client.put("k", "v", expiration_ttl=300) is False
        request.assert_not_awaited()


class TestPut:
    async def test_put_success(self):
        http, request = _http_with_response(200)
        ok = await _client(http).put(
            "cache:spoo.me:abc", '{"type":"redirect"}', expiration_ttl=300
        )
        assert ok is True
        method, url = request.await_args.args
        assert method == "PUT"
        # keys are URL-encoded into the path (CF requirement; emoji keys)
        assert url.endswith("/storage/kv/namespaces/ns/values/cache%3Aspoo.me%3Aabc")
        assert "/accounts/acc/" in url
        kwargs = request.await_args.kwargs
        assert kwargs["content"] == '{"type":"redirect"}'
        assert kwargs["params"] == {"expiration_ttl": 300}
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    async def test_emoji_key_is_url_encoded(self):
        """Emoji short codes must survive the REST path — raw emoji in a
        URL is undefined behavior; CF expects percent-encoding."""
        http, request = _http_with_response(200)
        assert await _client(http).put("cache:spoo.me:🚀", "v", expiration_ttl=60)
        url = request.await_args.args[1]
        assert url.endswith("/values/cache%3Aspoo.me%3A%F0%9F%9A%80")
        assert "🚀" not in url

    async def test_put_retries_on_5xx_then_succeeds(self):
        ok_resp = MagicMock(
            spec=httpx.Response, status_code=200, is_success=True, headers={}
        )
        bad_resp = MagicMock(
            spec=httpx.Response,
            status_code=500,
            is_success=False,
            text="oops",
            headers={},
        )
        http = MagicMock()
        http.request = AsyncMock(side_effect=[bad_resp, ok_resp])
        assert await _client(http).put("k", "v", expiration_ttl=60) is True
        assert http.request.await_count == 2

    async def test_put_gives_up_after_retries(self):
        http, request = _http_with_response(503)
        assert await _client(http).put("k", "v", expiration_ttl=60) is False
        assert request.await_count == 2  # max_retries

    async def test_put_4xx_fails_without_retry(self):
        """Auth/config 4xx can't be retried away — one attempt only."""
        http, request = _http_with_response(403)
        assert await _client(http).put("k", "v", expiration_ttl=60) is False
        request.assert_awaited_once()

    async def test_transport_error_never_raises(self):
        http = MagicMock()
        http.request = AsyncMock(side_effect=httpx.ConnectError("down"))
        assert await _client(http).put("k", "v", expiration_ttl=60) is False


class TestDelete:
    async def test_delete_success(self):
        http, request = _http_with_response(200)
        assert await _client(http).delete("cache:spoo.me:abc") is True
        assert request.await_args.args[0] == "DELETE"

    async def test_delete_404_is_success(self):
        """Idempotent: the entry being already gone is the desired state."""
        http, _ = _http_with_response(404)
        assert await _client(http).delete("k") is True


class TestBulkPut:
    async def test_bulk_put_success(self):
        http, request = _http_with_response(200)
        pairs = [("cache:spoo.me:a", "v1"), ("cache:spoo.me:b", "v2")]
        assert await _client(http).bulk_put(pairs, expiration_ttl=86_400) is True
        method, url = request.await_args.args
        assert method == "PUT"
        assert url.endswith("/storage/kv/namespaces/ns/bulk")
        assert request.await_args.kwargs["json"] == [
            {"key": "cache:spoo.me:a", "value": "v1", "expiration_ttl": 86_400},
            {"key": "cache:spoo.me:b", "value": "v2", "expiration_ttl": 86_400},
        ]

    async def test_bulk_put_without_ttl_omits_field(self):
        http, request = _http_with_response(200)
        assert await _client(http).bulk_put([("k", "v")]) is True
        assert request.await_args.kwargs["json"] == [{"key": "k", "value": "v"}]

    async def test_bulk_put_ttl_zero_is_preserved(self):
        # `is not None`, not truthiness — an explicit 0 must reach the wire
        # (matches single-key put's semantics).
        http, request = _http_with_response(200)
        assert await _client(http).bulk_put([("k", "v")], expiration_ttl=0) is True
        assert request.await_args.kwargs["json"] == [
            {"key": "k", "value": "v", "expiration_ttl": 0}
        ]

    async def test_bulk_put_partial_failure_reports_false(self):
        """CF can 200 with result.unsuccessful_keys — the bool must not lie."""
        http, _ = _http_with_response(
            200,
            json_body={
                "success": True,
                "result": {"successful_key_count": 1, "unsuccessful_keys": ["k2"]},
            },
        )
        assert await _client(http).bulk_put([("k1", "v"), ("k2", "v")]) is False

    async def test_empty_pairs_no_op_without_http_call(self):
        http, request = _http_with_response(200)
        assert await _client(http).bulk_put([]) is True
        request.assert_not_awaited()

    async def test_bulk_put_chunks_at_cf_limit(self):
        http, request = _http_with_response(200)
        pairs = [(f"k{i}", "v") for i in range(10_001)]
        assert await _client(http).bulk_put(pairs) is True
        assert request.await_count == 2
        first, second = request.await_args_list
        assert len(first.kwargs["json"]) == 10_000
        assert len(second.kwargs["json"]) == 1

    async def test_bulk_put_unconfigured_returns_false(self):
        http, request = _http_with_response(200)
        assert await _client(http, api_token=None).bulk_put([("k", "v")]) is False
        request.assert_not_awaited()


class TestBulkDelete:
    async def test_bulk_delete_success(self):
        http, request = _http_with_response(200)
        keys = ["cache:spoo.me:a", "cache:spoo.me:b"]
        assert await _client(http).bulk_delete(keys) is True
        method, url = request.await_args.args
        assert method == "POST"
        assert url.endswith("/storage/kv/namespaces/ns/bulk/delete")
        assert request.await_args.kwargs["json"] == keys

    async def test_empty_keys_no_op_without_http_call(self):
        http, request = _http_with_response(200)
        assert await _client(http).bulk_delete([]) is True
        request.assert_not_awaited()

    async def test_bulk_delete_4xx_fails_without_retry(self):
        http, request = _http_with_response(403)
        assert await _client(http).bulk_delete(["k"]) is False
        request.assert_awaited_once()

    async def test_bulk_delete_transport_error_never_raises(self):
        http = MagicMock()
        http.request = AsyncMock(side_effect=httpx.ConnectError("down"))
        assert await _client(http).bulk_delete(["k"]) is False

    async def test_bulk_delete_gives_up_after_retries_on_5xx(self):
        http, request = _http_with_response(503)
        assert await _client(http).bulk_delete(["k"]) is False
        assert request.await_count == 2  # max_retries

    async def test_bulk_delete_partial_failure_reports_false(self):
        http, _ = _http_with_response(
            200,
            json_body={
                "success": True,
                "result": {"successful_key_count": 1, "unsuccessful_keys": ["k2"]},
            },
        )
        assert await _client(http).bulk_delete(["k1", "k2"]) is False

    async def test_bulk_delete_non_json_2xx_trusts_status(self):
        """The local emulator answers 2xx without CF's JSON envelope."""
        http, _ = _http_with_response(200)
        http.request.return_value.json = MagicMock(side_effect=ValueError("no json"))
        assert await _client(http).bulk_delete(["k"]) is True


class TestBulkFallback:
    """wrangler dev's Explorer API mirrors only the single-key routes;
    a 404 on the bulk path degrades to per-key calls so the zero-cloud
    local loop still exercises edge writes."""

    def _http_404_then_ok(self):
        missing = MagicMock(
            spec=httpx.Response,
            status_code=404,
            is_success=False,
            text="404 Not Found",
            headers={},
        )
        ok = MagicMock(
            spec=httpx.Response, status_code=200, is_success=True, headers={}
        )
        http = MagicMock()
        http.request = AsyncMock(side_effect=[missing, ok, ok])
        return http, http.request

    async def test_bulk_delete_route_404_falls_back_to_per_key(self):
        http, request = self._http_404_then_ok()
        keys = ["cache:spoo.local:a", "cache:spoo.local:b"]
        assert await _client(http).bulk_delete(keys) is True
        assert request.await_count == 3  # 1 bulk attempt + 2 single deletes
        single_urls = [call.args[1] for call in request.await_args_list[1:]]
        assert all("/values/" in url for url in single_urls)
        assert [call.args[0] for call in request.await_args_list[1:]] == [
            "DELETE",
            "DELETE",
        ]

    async def test_bulk_put_route_404_falls_back_to_per_key_with_ttl(self):
        http, request = self._http_404_then_ok()
        pairs = [("cache:spoo.local:a", "v1"), ("cache:spoo.local:b", "v2")]
        assert await _client(http).bulk_put(pairs, expiration_ttl=86_400) is True
        assert request.await_count == 3
        for call in request.await_args_list[1:]:
            assert call.args[0] == "PUT"
            assert "/values/" in call.args[1]
            assert call.kwargs["params"] == {"expiration_ttl": 86_400}


class TestApiBaseOverride:
    async def test_local_emulator_base_replaces_account_scoped_url(self):
        """api_base points the client at wrangler dev's Explorer API,
        which mirrors /storage/kv/... without the /accounts prefix."""
        http, request = _http_with_response(200)
        client = _client(
            http,
            account_id=None,
            api_base="http://localhost:8787/cdn-cgi/explorer/api",
        )
        assert client.is_configured is True
        assert await client.put("k", "v", expiration_ttl=60) is True
        url = request.await_args.args[1]
        assert url == (
            "http://localhost:8787/cdn-cgi/explorer/api"
            "/storage/kv/namespaces/ns/values/k"
        )
        assert "/accounts/" not in url

    async def test_unconfigured_without_account_or_base(self):
        http, request = _http_with_response(200)
        client = _client(http, account_id=None, api_base=None)
        assert client.is_configured is False
        assert await client.put("k", "v", expiration_ttl=60) is False
        request.assert_not_awaited()
