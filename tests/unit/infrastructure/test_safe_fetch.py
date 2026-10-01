"""SSRF-guard tests for infrastructure/safe_fetch.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.safe_fetch import (
    FetchHardError,
    _is_public,
    _read_body,
    fetch_public_image,
    resolve_public_ip,
)

# ── _is_public matrix ─────────────────────────────────────────────────────────


class TestIsPublic:
    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",
            "172.16.5.5",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",  # cloud metadata
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd00::1",  # ULA
            "::ffff:10.0.0.1",  # IPv4-mapped bypass attempt
            "224.0.0.1",  # multicast
            "100.64.0.1",  # CGNAT — cloud pod IPs, is_global=False
            "2002:c058:6301::1",  # 6to4
        ],
    )
    def test_private_and_special_rejected(self, ip):
        assert _is_public(ip) is False

    @pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1::1"])
    def test_public_accepted(self, ip):
        assert _is_public(ip) is True


# ── resolution guard ──────────────────────────────────────────────────────────


class TestResolvePublicIp:
    @pytest.mark.asyncio
    async def test_literal_private_ip_rejected(self):
        with pytest.raises(FetchHardError):
            await resolve_public_ip("169.254.169.254")

    @pytest.mark.asyncio
    async def test_literal_public_ip_accepted(self):
        assert await resolve_public_ip("93.184.216.34") == "93.184.216.34"

    @pytest.mark.asyncio
    async def test_mixed_record_set_rejected(self):
        # ANY private record fails the host — the rebinding/split-horizon shape.
        def _answer(host, rdtype):
            rec_public = AsyncMock()
            rec_public.to_text = lambda: "93.184.216.34"
            rec_private = AsyncMock()
            rec_private.to_text = lambda: "10.0.0.1"
            answer = AsyncMock()
            if rdtype == "A":
                answer.__iter__ = lambda self: iter([rec_public, rec_private])
            else:
                answer.__iter__ = lambda self: iter([])
            return answer

        with (
            patch(
                "app.infrastructure.safe_fetch.dns.asyncresolver.resolve",
                new=AsyncMock(side_effect=_answer),
            ),
            pytest.raises(FetchHardError, match="non-public"),
        ):
            await resolve_public_ip("evil.example.com")


# ── fetch-level guards (no network: fail before connecting) ─────────────────


class TestFetchGuards:
    @pytest.mark.asyncio
    async def test_http_url_rejected(self):
        with pytest.raises(FetchHardError, match="non-https"):
            await fetch_public_image("http://example.com/a.png")

    @pytest.mark.asyncio
    async def test_private_host_rejected_before_any_connection(self):
        with pytest.raises(FetchHardError):
            await fetch_public_image("https://127.0.0.1/a.png")

    @pytest.mark.asyncio
    async def test_metadata_endpoint_rejected(self):
        with pytest.raises(FetchHardError):
            await fetch_public_image("https://169.254.169.254/latest/meta-data")

    @pytest.mark.asyncio
    async def test_request_disables_gzip(self):
        # A compressed bomb would inflate past max_bytes in one read, so the
        # outgoing request must ask for identity encoding.
        captured = {}

        class _Resp:
            status_code = 200
            headers = {"content-type": "image/png", "content-length": "3"}  # noqa: RUF012

            async def aiter_bytes(self):
                yield b"abc"

            async def aclose(self):
                pass

        async def _fake_send(self, request, **kwargs):
            captured["accept_encoding"] = request.headers.get("accept-encoding")
            return _Resp()

        with (
            patch(
                "app.infrastructure.safe_fetch.resolve_public_ip",
                new=AsyncMock(return_value="93.184.216.34"),
            ),
            patch("httpx.AsyncClient.send", new=_fake_send),
        ):
            await fetch_public_image("https://example.com/a.png")
        assert captured["accept_encoding"] == "identity"


class TestReadBodyStopMarker:
    """``stop_after`` ends the read at </head> so an HTML caller pays for the
    head, not the cap. Heads are not reliably small: youtube puts ~700KB of
    inline JSON before its meta tags, which a 512KB cap silently cut off."""

    @staticmethod
    def _resp(chunks):
        resp = MagicMock()

        async def aiter_bytes():
            for chunk in chunks:
                yield chunk

        resp.aiter_bytes = aiter_bytes
        return resp

    @pytest.mark.asyncio
    async def test_read_stops_at_the_marker(self):
        body = await _read_body(
            self._resp([b"<head><title>x</title></head>", b"<body>" + b"z" * 5000]),
            max_bytes=1_000_000,
            truncate_over_cap=True,
            stop_after=b"</head>",
        )
        assert bytes(body) == b"<head><title>x</title></head>"

    @pytest.mark.asyncio
    async def test_marker_split_across_chunks_is_still_found(self):
        body = await _read_body(
            self._resp([b"<head>a</he", b"ad><body>ignored"]),
            max_bytes=1_000_000,
            truncate_over_cap=True,
            stop_after=b"</head>",
        )
        assert bytes(body) == b"<head>a</head>"

    @pytest.mark.asyncio
    async def test_tags_past_the_old_512kb_cap_survive(self):
        """The youtube shape: a huge head, then the tags."""
        head = b"<head>" + b"j" * 700_000 + b'<meta property="og:title"></head>'
        body = await _read_body(
            self._resp([head, b"<body>" + b"z" * 600_000]),
            max_bytes=1_048_576,
            truncate_over_cap=True,
            stop_after=b"</head>",
        )
        assert b"og:title" in bytes(body)

    @pytest.mark.asyncio
    async def test_a_missing_marker_still_honours_the_cap(self):
        body = await _read_body(
            self._resp([b"x" * 900, b"y" * 900]),
            max_bytes=1_000,
            truncate_over_cap=True,
            stop_after=b"</head>",
        )
        assert len(body) == 1_000

    @pytest.mark.asyncio
    async def test_without_a_marker_the_whole_body_is_read(self):
        body = await _read_body(
            self._resp([b"<head></head>", b"<body>tail"]),
            max_bytes=1_000,
            truncate_over_cap=True,
        )
        assert bytes(body) == b"<head></head><body>tail"
