"""Tests for stdlib magic-byte image sniffing + dimension extraction."""

from __future__ import annotations

import struct
import zlib

from app.shared.image_sniff import EXT, MIME, sniff_image

# ── Real minimal fixtures ─────────────────────────────────────────────────────


def _png(width=3, height=2) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr_chunk = (
        struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return b"\x89PNG\r\n\x1a\n" + ihdr_chunk


def _jpeg(width=4, height=3) -> bytes:
    # SOI + APP0 (JFIF) + SOF0 with dims
    app0 = (
        b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
    )
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof0


def _gif(width=5, height=6) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 4


def _webp_vp8x(width=1200, height=630) -> bytes:
    payload = (
        b"WEBPVP8X"
        + struct.pack("<I", 10)
        + b"\x00" * 4
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


class TestSniff:
    def test_png(self):
        info = sniff_image(_png(3, 2))
        assert info is not None
        assert (info.format, info.width, info.height) == ("png", 3, 2)

    def test_jpeg(self):
        info = sniff_image(_jpeg(4, 3))
        assert info is not None
        assert (info.format, info.width, info.height) == ("jpeg", 4, 3)

    def test_gif(self):
        info = sniff_image(_gif(5, 6))
        assert info is not None
        assert (info.format, info.width, info.height) == ("gif", 5, 6)

    def test_webp_vp8x(self):
        info = sniff_image(_webp_vp8x(1200, 630))
        assert info is not None
        assert (info.format, info.width, info.height) == ("webp", 1200, 630)

    def test_unrecognized_returns_none(self):
        assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
        assert sniff_image(b"GIF12a??") is None
        assert sniff_image(b"") is None

    def test_truncated_png_still_identifies_format(self):
        # Magic matched but no IHDR — the security gate holds, dims degrade.
        info = sniff_image(b"\x89PNG\r\n\x1a\n\x00\x00")
        assert info is not None
        assert info.format == "png"
        assert info.width is None and info.height is None

    def test_mime_and_ext_maps_cover_all_formats(self):
        for fmt in ("png", "jpeg", "webp", "gif"):
            assert fmt in MIME and fmt in EXT
