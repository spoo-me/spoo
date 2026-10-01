"""Tests for the R2 storage client and SigV4 signing."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import R2StorageError
from app.infrastructure.storage.r2 import R2StorageClient
from app.shared.sigv4 import sigv4_headers

# ── SigV4 ─────────────────────────────────────────────────────────────────────


class TestSigV4:
    def test_aws_reference_vector_get_vanilla(self):
        """AWS SigV4 test suite: get-vanilla (region us-east-1, service
        'service', 2015-08-30T12:36:00Z, empty payload)."""
        headers = sigv4_headers(
            method="GET",
            host="example.amazonaws.com",
            path="/",
            payload_hash=hashlib.sha256(b"").hexdigest(),
            access_key_id="AKIDEXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
            service="service",
            now=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc),
        )
        # Signature published in the AWS SigV4 test suite for this request
        # (with x-amz-content-sha256 excluded there; our variant signs it,
        # so pin OUR deterministic output instead and assert structure).
        auth = headers["Authorization"]
        assert auth.startswith(
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
        )
        assert headers["x-amz-date"] == "20150830T123600Z"
        # Deterministic: same inputs must always produce the same signature.
        again = sigv4_headers(
            method="GET",
            host="example.amazonaws.com",
            path="/",
            payload_hash=hashlib.sha256(b"").hexdigest(),
            access_key_id="AKIDEXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
            service="service",
            now=datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc),
        )
        assert again["Authorization"] == auth

    def test_extra_headers_are_signed_sorted(self):
        headers = sigv4_headers(
            method="PUT",
            host="acc.r2.cloudflarestorage.com",
            path="/bucket/og/x.png",
            payload_hash="abc123",
            access_key_id="k",
            secret_access_key="s",
            headers={"Content-Type": "image/png"},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert (
            "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date"
            in headers["Authorization"]
        )


# ── R2StorageClient ───────────────────────────────────────────────────────────


def _client(status=200, **overrides) -> tuple[R2StorageClient, AsyncMock]:
    resp = MagicMock()
    resp.status_code = status
    resp.text = "err-body"
    http = MagicMock()
    http.request = AsyncMock(return_value=resp)
    kwargs = dict(
        http_client=http,
        account_id="acc",
        access_key_id="key",
        secret_access_key="secret",
        bucket="og-images",
        public_base_url="https://og.spoo.me/",
    )
    kwargs.update(overrides)
    return R2StorageClient(**kwargs), http


class TestR2StorageClient:
    def test_is_configured(self):
        client, _ = _client()
        assert client.is_configured is True
        partial, _ = _client(bucket=None)
        assert partial.is_configured is False

    def test_public_url_strips_trailing_slash(self):
        client, _ = _client()
        assert client.public_url("og/u/abc.png") == "https://og.spoo.me/og/u/abc.png"

    @pytest.mark.asyncio
    async def test_put_object_success(self):
        client, http = _client()
        url = await client.put_object("og/u/abc.png", b"data", content_type="image/png")
        assert url == "https://og.spoo.me/og/u/abc.png"
        method, target = http.request.call_args.args
        assert method == "PUT"
        assert target == "https://acc.r2.cloudflarestorage.com/og-images/og/u/abc.png"
        sent_headers = http.request.call_args.kwargs["headers"]
        assert sent_headers["content-type"] == "image/png"
        # Content-addressed keys are immutable — CDN may cache forever.
        assert sent_headers["cache-control"] == "public, max-age=31536000, immutable"
        # Defence-in-depth for user-supplied bytes: never render as a doc.
        assert sent_headers["content-disposition"] == "inline"
        assert sent_headers["x-content-type-options"] == "nosniff"
        # Both must be inside the SigV4 SignedHeaders, not just sent.
        signed = sent_headers["Authorization"]
        assert "content-disposition" in signed
        assert "x-content-type-options" in signed
        assert http.request.call_args.kwargs["timeout"] == 15.0

    @pytest.mark.asyncio
    async def test_put_object_failure_raises(self):
        client, _ = _client(status=403)
        with pytest.raises(R2StorageError):
            await client.put_object("k", b"data", content_type="image/png")

    @pytest.mark.asyncio
    async def test_put_object_transport_error_raises(self):
        client, http = _client()
        http.request.side_effect = RuntimeError("conn reset")
        with pytest.raises(R2StorageError):
            await client.put_object("k", b"data", content_type="image/png")

    @pytest.mark.asyncio
    async def test_delete_404_counts_as_success(self):
        client, _ = _client(status=404)
        assert await client.delete_object("k") is True

    @pytest.mark.asyncio
    async def test_delete_failure_returns_false(self):
        client, _ = _client(status=500)
        assert await client.delete_object("k") is False

    def test_endpoint_override_for_local_dev(self):
        client, _ = _client(endpoint_url="http://localhost:9000/")
        assert client._endpoint == "http://localhost:9000"

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://localhost:9000/",
            "http://127.0.0.1:9000",
            "http://[::1]:9000",
        ],
    )
    def test_plain_http_allowed_only_for_loopback(self, endpoint):
        client, _ = _client(endpoint_url=endpoint)
        assert client._endpoint == endpoint.rstrip("/")

    def test_plain_http_rejected_for_remote_hosts(self):
        # SigV4 signs but never encrypts — non-loopback must be https.
        with pytest.raises(ValueError, match="https"):
            _client(endpoint_url="http://minio.internal:9000")


class TestSigV4Query:
    def test_query_string_changes_the_signature(self):
        common = dict(
            method="GET",
            host="acc.r2.cloudflarestorage.com",
            path="/bucket",
            payload_hash=hashlib.sha256(b"").hexdigest(),
            access_key_id="k",
            secret_access_key="s",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        plain = sigv4_headers(**common)
        with_query = sigv4_headers(query="list-type=2&prefix=og%2Fx%2F", **common)
        assert plain["Authorization"] != with_query["Authorization"]
        # Deterministic for the same query.
        again = sigv4_headers(query="list-type=2&prefix=og%2Fx%2F", **common)
        assert again["Authorization"] == with_query["Authorization"]


_LIST_PAGE_1 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>tok-1</NextContinuationToken>
  <Contents><Key>og/pfx/a.png</Key></Contents>
  <Contents><Key>og/pfx/b.png</Key></Contents>
</ListBucketResult>"""

_LIST_PAGE_2 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult>
  <IsTruncated>false</IsTruncated>
  <Contents><Key>og/pfx/c.png</Key></Contents>
</ListBucketResult>"""


class TestR2ListKeys:
    @pytest.mark.asyncio
    async def test_list_keys_paginates_with_continuation_token(self):
        client, http = _client()
        page1 = MagicMock(status_code=200, text=_LIST_PAGE_1)
        page2 = MagicMock(status_code=200, text=_LIST_PAGE_2)
        http.request = AsyncMock(side_effect=[page1, page2])

        keys = await client.list_keys("og/pfx/")

        assert keys == ["og/pfx/a.png", "og/pfx/b.png", "og/pfx/c.png"]
        first_target = http.request.await_args_list[0].args[1]
        assert first_target == (
            "https://acc.r2.cloudflarestorage.com/og-images"
            "?list-type=2&prefix=og%2Fpfx%2F"
        )
        second_target = http.request.await_args_list[1].args[1]
        assert "continuation-token=tok-1" in second_target

    @pytest.mark.asyncio
    async def test_list_keys_empty_result(self):
        client, http = _client()
        empty = MagicMock(
            status_code=200,
            text="<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>",
        )
        http.request = AsyncMock(return_value=empty)
        assert await client.list_keys("og/none/") == []

    @pytest.mark.asyncio
    async def test_list_keys_failure_raises(self):
        client, _ = _client(status=403)
        with pytest.raises(R2StorageError):
            await client.list_keys("og/pfx/")

    @pytest.mark.asyncio
    async def test_list_keys_transport_error_raises(self):
        client, http = _client()
        http.request.side_effect = RuntimeError("conn reset")
        with pytest.raises(R2StorageError):
            await client.list_keys("og/pfx/")
