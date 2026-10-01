"""Outcome tests for the async og:image validator consumer."""

from __future__ import annotations

import struct
import zlib
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from app.infrastructure.safe_fetch import (
    FetchDeniedError,
    FetchedBody,
    FetchHardError,
    FetchTransientError,
)
from app.services.meta_tags.events import MetaImageValidateEvent
from app.services.meta_tags.validator import MetaImageValidator

URL_ID = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")


def _png_bytes(width=1200, height=630) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = (
        struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return b"\x89PNG\r\n\x1a\n" + chunk


def _payload(image_url="https://ext.example.com/og.png") -> dict:
    return MetaImageValidateEvent(
        url_id=str(URL_ID),
        alias="abc1234",
        domain="spoo.me",
        image_url=image_url,
    ).model_dump(mode="json")


def _validator():
    repo = AsyncMock()
    repo.record_meta_image_validation = AsyncMock(return_value=True)
    repo.clear_meta_image = AsyncMock(return_value=True)
    cache = AsyncMock()
    return MetaImageValidator(repo, cache), repo, cache


class TestConsume:
    @pytest.mark.asyncio
    async def test_success_records_dims_and_invalidates(self):
        validator, repo, cache = _validator()
        body = FetchedBody(_png_bytes(1200, 630), "image/png", "https://x/og.png")
        with patch(
            "app.services.meta_tags.validator.fetch_public_image",
            new=AsyncMock(return_value=body),
        ):
            await validator.consume(_payload())
        args = repo.record_meta_image_validation.call_args.args
        assert args[0] == URL_ID
        assert args[1] == "https://ext.example.com/og.png"
        assert args[2]["width"] == 1200 and args[2]["height"] == 630
        cache.invalidate.assert_awaited_once_with("abc1234", "spoo.me")

    @pytest.mark.asyncio
    async def test_hard_failure_clears_image(self):
        validator, repo, cache = _validator()
        with patch(
            "app.services.meta_tags.validator.fetch_public_image",
            new=AsyncMock(side_effect=FetchHardError("content-type 'text/html'")),
        ):
            await validator.consume(_payload())
        repo.clear_meta_image.assert_awaited_once()
        repo.record_meta_image_validation.assert_not_called()
        cache.invalidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_denied_fetch_keeps_image(self):
        # 401/403 to OUR UA (WAF/hotlink protection) is indeterminate —
        # preview crawlers fetch with allowlisted UAs. Never clear on it.
        validator, repo, cache = _validator()
        with patch(
            "app.services.meta_tags.validator.fetch_public_image",
            new=AsyncMock(side_effect=FetchDeniedError("status 403")),
        ):
            await validator.consume(_payload())
        repo.clear_meta_image.assert_not_called()
        repo.record_meta_image_validation.assert_not_called()
        cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_failure_propagates_for_retry(self):
        validator, repo, _ = _validator()
        with (
            patch(
                "app.services.meta_tags.validator.fetch_public_image",
                new=AsyncMock(side_effect=FetchTransientError("timeout")),
            ),
            pytest.raises(FetchTransientError),
        ):
            await validator.consume(_payload())
        repo.clear_meta_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_payload_dropped_silently(self):
        validator, repo, _cache = _validator()
        await validator.consume({"nonsense": True})
        await validator.consume("not-a-dict")
        repo.clear_meta_image.assert_not_called()
        repo.record_meta_image_validation.assert_not_called()

    @pytest.mark.asyncio
    async def test_cas_miss_skips_invalidation(self):
        # User replaced the image while we fetched — writes were no-ops.
        validator, repo, cache = _validator()
        repo.record_meta_image_validation.return_value = False
        body = FetchedBody(_png_bytes(), "image/png", "https://x/og.png")
        with patch(
            "app.services.meta_tags.validator.fetch_public_image",
            new=AsyncMock(return_value=body),
        ):
            await validator.consume(_payload())
        cache.invalidate.assert_not_called()
