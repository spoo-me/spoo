"""Destination meta-tag parser for GET /api/v1/metadata (prefill helper).

Stdlib HTMLParser — lenient with broken markup, zero new dependencies.
Parsing stops at </head> (or the first <body>) so a 5MB page costs the
same as a 5KB one; preview-relevant tags legally live in <head> anyway.

Quirk handling, learned from how sites actually write these tags:
  - og:* uses ``property=`` and twitter:* uses ``name=`` per spec, but
    sites mix them up constantly — both attributes are accepted for both.
  - First tag wins per key (the OG spec's resolution rule).
  - Relative og:image URLs resolve against the FINAL post-redirect URL.
  - Entities decode via convert_charrefs (HTMLParser default).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_INTERESTING = ("og:", "twitter:")
_INTERESTING_NAMES = {"description", "theme-color"}


@dataclass(frozen=True)
class ParsedMeta:
    """Normalized best-pick fields + the raw tag families."""

    title: str | None
    description: str | None
    image: str | None
    color: str | None
    site_name: str | None
    html_title: str | None
    html_description: str | None
    favicon: str | None
    og: dict[str, str]
    twitter: dict[str, str]


class _StopParsing(Exception):
    pass


class _MetaCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.icons: list[tuple[str, str, str]] = []  # (rel, href, sizes)
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "body":
            raise _StopParsing
        if tag == "title":
            self._in_title = True
            return
        if tag == "link":
            attr = dict(attrs)
            rel = (attr.get("rel") or "").strip().lower()
            href = (attr.get("href") or "").strip()
            tokens = rel.split()
            if href and (
                "icon" in tokens
                or any(t.startswith("apple-touch-icon") for t in tokens)
            ):
                self.icons.append((rel, href, (attr.get("sizes") or "").strip()))
            return
        if tag != "meta":
            return
        attr = dict(attrs)
        key = (attr.get("property") or attr.get("name") or "").strip().lower()
        content = attr.get("content")
        if not key or content is None:
            return
        if key.startswith(_INTERESTING) or key in _INTERESTING_NAMES:
            self.meta.setdefault(key, content.strip())  # first tag wins

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "head":
            raise _StopParsing

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def parse_meta_tags(html: str, base_url: str) -> ParsedMeta:
    """Parse *html* (already decoded) fetched from *base_url* (final URL)."""
    collector = _MetaCollector()
    with contextlib.suppress(_StopParsing):
        collector.feed(html)

    meta = collector.meta
    og = {k.removeprefix("og:"): v for k, v in meta.items() if k.startswith("og:")}
    twitter = {
        k.removeprefix("twitter:"): v
        for k, v in meta.items()
        if k.startswith("twitter:")
    }
    html_title = "".join(collector.title_parts).strip() or None

    image = og.get("image") or twitter.get("image")
    if image:
        image = urljoin(base_url, image.strip())
        if not image.startswith("https://"):
            image = None  # http/data/relative-garbage images are useless to us

    color = meta.get("theme-color")
    if color and not _HEX_COLOR_RE.match(color.strip()):
        color = None

    return ParsedMeta(
        title=og.get("title") or twitter.get("title") or html_title,
        description=(
            og.get("description")
            or twitter.get("description")
            or meta.get("description")
        ),
        image=image,
        color=color.strip() if color else None,
        site_name=og.get("site_name"),
        html_title=html_title,
        html_description=meta.get("description"),
        favicon=_pick_favicon(collector.icons, base_url),
        og=og,
        twitter=twitter,
    )


def _pick_favicon(icons: list[tuple[str, str, str]], base_url: str) -> str | None:
    """Best declared icon, else the /favicon.ico convention; https only."""

    def score(icon: tuple[str, str, str]) -> int:
        rel, _href, sizes = icon
        if "apple-touch-icon" in rel:
            return 180
        m = re.match(r"(\d+)", sizes)
        return int(m.group(1)) if m else 16

    href = max(icons, key=score)[1] if icons else "/favicon.ico"
    resolved = urljoin(base_url, href)
    return resolved if resolved.startswith("https://") else None
