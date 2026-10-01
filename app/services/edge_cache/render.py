"""Request-free rendering of meta_preview.html for the edge KV write-through.

Renders the SAME template + context the origin serving branch uses
(services/meta_preview.py builds the context), but without a Request —
promotion and the write-through run outside the HTTP request cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.infrastructure.templates import templates
from app.services.meta_preview import build_preview_context

if TYPE_CHECKING:
    from app.infrastructure.cache.url_cache import UrlCacheData


def render_meta_preview(url: UrlCacheData) -> str:
    """Return the final HTML string stored in the KV entry's og_html."""
    template = templates.env.get_template("meta_preview.html")
    return template.render(build_preview_context(url, auto_redirect=True))
