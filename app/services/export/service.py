"""
ExportService — data export for v2 stats.

Composes StatsService for data, then delegates serialisation to the
injected formatter registry.  Returns an ExportResult so the route can
build the file response without any framework coupling here.

Adding a new export format never requires touching this file:
create a class implementing ExportFormatter and register it in
default_formatters() (services/export/formatters.py).

The legacy v1 export (GET /export/<code>/<format> page) continues to use
utils/export_utils.py directly — that path is unaffected by this service.
"""

from __future__ import annotations

import re

from app.errors import ValidationError
from app.infrastructure.logging import get_logger
from app.schemas.dto.requests.stats import ExportQuery, LinkExportQuery
from app.schemas.models.url import UrlV2Doc
from app.schemas.results import ExportResult
from app.services.export.protocol import ExportFormatter
from app.services.stats_service import StatsService

log = get_logger(__name__)

# Content-Disposition rides the wire latin-1 encoded, so only aliases that
# are plain URL-safe tokens may appear in a filename — emoji aliases fall
# back to the id.
_FILENAME_SAFE_ALIAS = re.compile(r"[A-Za-z0-9_-]+")


class ExportService:
    """Export service for v2 stats.

    Args:
        stats_service: StatsService instance used to retrieve analytics data.
        formatters:    Registry mapping format name → ExportFormatter instance.
                        Inject via ``default_formatters()`` at the composition root.
    """

    def __init__(
        self,
        stats_service: StatsService,
        formatters: dict[str, ExportFormatter],
    ) -> None:
        self._stats = stats_service
        self._formatters = formatters

    async def export(
        self,
        query: ExportQuery,
        owner_id: str | None,
    ) -> ExportResult:
        """Retrieve stats and serialise them in the requested format.

        Args:
            query:    Validated ExportQuery DTO with format and stats parameters.
            owner_id: String user ID of the authenticated caller.

        Raises:
            ValidationError: Unknown format.
            Propagates all errors from StatsService.query().
        """
        formatter = self._formatter(query.format)

        data = await self._stats.query(query, owner_id)

        content = formatter.serialize(data)

        log.info(
            "export_generated",
            format=query.format,
            size_bytes=len(content),
        )
        return ExportResult(
            content=content, mimetype=formatter.mimetype, filename=formatter.filename
        )

    async def export_link(
        self,
        query: LinkExportQuery,
        url: UrlV2Doc,
    ) -> ExportResult:
        """Export per-link stats for an already-resolved owned URL.

        The route layer owns resolution and access control (same resolve-first
        contract as StatsService.query_link). The filename carries the alias.

        Args:
            query: Validated LinkExportQuery DTO.
            url:   The owned URL document resolved by the route.

        Raises:
            ValidationError: Unknown format.
            Propagates all errors from StatsService.query_link().
        """
        formatter = self._formatter(query.format)

        data = await self._stats.query_link(query, url)

        content = formatter.serialize(data)

        log.info(
            "export_generated",
            format=query.format,
            url_id=str(url.id),
            size_bytes=len(content),
        )
        return ExportResult(
            content=content,
            mimetype=formatter.mimetype,
            filename=_link_filename(formatter.filename, url),
        )

    def _formatter(self, fmt: str) -> ExportFormatter:
        if fmt not in self._formatters:
            raise ValidationError(
                f"invalid format — must be one of: {', '.join(sorted(self._formatters))}"
            )
        return self._formatters[fmt]


def _link_filename(base: str, url: UrlV2Doc) -> str:
    """Derive the per-link filename from the formatter's account one."""
    tag = url.alias if _FILENAME_SAFE_ALIAS.fullmatch(url.alias or "") else str(url.id)
    return base.replace("spoo-me-export", f"spoo-me-export-{tag}", 1)
