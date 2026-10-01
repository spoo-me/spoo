"""Stdlib magic-byte image sniffing + best-effort dimensions.

``imghdr`` was removed in Python 3.13 and Pillow is deliberately not a
dependency — this covers exactly the formats preview crawlers render.
The MAGIC MATCH is the security gate (upload type validation); the
dimensions are best-effort metadata (og:image:width/height) and return
None on truncated/exotic files without failing the sniff.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Literal

MIME: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
EXT: dict[str, str] = {"png": "png", "jpeg": "jpg", "webp": "webp", "gif": "gif"}


@dataclass(frozen=True)
class ImageInfo:
    format: Literal["png", "jpeg", "webp", "gif"]
    width: int | None
    height: int | None


def sniff_image(data: bytes) -> ImageInfo | None:
    """Identify the image format from magic bytes; None if unrecognized."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return ImageInfo("png", w, h)
        return ImageInfo("png", None, None)
    if data[:3] == b"\xff\xd8\xff":
        dims = _jpeg_dimensions(data)
        return ImageInfo("jpeg", *(dims or (None, None)))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        dims = _webp_dimensions(data)
        return ImageInfo("webp", *(dims or (None, None)))
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return ImageInfo("gif", w, h)
        return ImageInfo("gif", None, None)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to the first SOF marker (0xC0-0xCF excl. C4/C8/CC)."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        # Lossy: 14-bit little-endian dims at bytes 26/28 (after frame tag).
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return w, h
    if chunk == b"VP8L" and len(data) >= 25:
        # Lossless: bit-packed 14-bit dims starting at byte 21.
        bits = struct.unpack("<I", data[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    if chunk == b"VP8X" and len(data) >= 30:
        # Extended: 24-bit little-endian canvas-minus-one at bytes 24/27.
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    return None
