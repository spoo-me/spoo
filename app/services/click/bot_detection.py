"""
Bot detection utilities — framework-agnostic.

Combines two detection methods:
1. ``crawlerdetect`` library (signature-based)
2. BOT_USER_AGENTS regex patterns loaded lazily from ``bot_user_agents.txt``

The pattern file is loaded once via ``functools.lru_cache`` so there is no
import-time I/O (unlike the original ``utils/url_utils.py``).
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING

from crawlerdetect import CrawlerDetect

from app.schemas.models.url import SchemaVersion

if TYPE_CHECKING:
    from app.infrastructure.cache.url_cache import UrlCacheData

_BOT_UA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "bot_user_agents.txt"
)

# Package data/ (same home as bot_user_agents.txt above).
# Canonical copy; edge/spoo-edge-cache/contract/preview_bots.json must stay
# byte-identical — pinned by tests on both sides.
_PREVIEW_BOTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "preview_bots.json",
)

_crawler_detect = CrawlerDetect()


@lru_cache(maxsize=1)
def _load_bot_user_agents() -> list[str]:
    """Load and cache bot UA patterns from ``bot_user_agents.txt``.

    Returns an empty list if the file cannot be read so that callers
    degrade gracefully rather than raising at import time.
    """
    try:
        with open(_BOT_UA_PATH) as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


@lru_cache(maxsize=1)
def _preview_bots_res() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Compile the preview-crawler allowlist once per process."""
    with open(_PREVIEW_BOTS_PATH) as fh:
        data = json.load(fh)
    ci = re.compile("|".join(re.escape(t) for t in data["tokens"]), re.IGNORECASE)
    cs = re.compile("|".join(re.escape(t) for t in data["tokens_cs"]))
    return ci, cs


def wants_preview(method: str, user_agent: str, *, bot_param: bool = False) -> bool:
    """Should this request get the custom OG page instead of the 302?

    Positive allowlist of link-preview crawlers only — NOT generic bot
    detection. Search/AI crawlers, scrapers and humans all fall through to
    the 302 (search engines keep destination SEO/link equity; a missed
    preview bot just follows the redirect and shows the destination's own
    tags — today's behavior). HEAD ⇒ preview: no real user HEADs; email
    scanners and link expanders do. Only ever called for og-enabled links.
    """
    if bot_param or method == "HEAD":
        return True
    if not user_agent:
        return False
    ci, cs = _preview_bots_res()
    return bool(ci.search(user_agent) or cs.search(user_agent))


def is_bot_request(user_agent: str) -> bool:
    """Return True if *user_agent* looks like an automated crawler or bot.

    Checks both the ``CrawlerDetect`` library signature database and the
    local ``bot_user_agents.txt`` regex patterns.

    Args:
        user_agent: The ``User-Agent`` header value.

    Returns:
        ``True`` if a bot signature is detected.
    """
    if _crawler_detect.isCrawler(user_agent):
        return True
    bot_patterns = _load_bot_user_agents()
    return any(
        re.search(pattern, user_agent, re.IGNORECASE) for pattern in bot_patterns
    )


def get_bot_name(user_agent: str) -> str | None:
    """Return the name/pattern of the detected bot, or ``None`` for humans.

    Tries ``CrawlerDetect.getMatches()`` first, then falls back to the
    first matching pattern from ``bot_user_agents.txt``.

    Args:
        user_agent: The ``User-Agent`` header value.

    Returns:
        A string identifying the bot, or ``None`` if no bot was detected.
    """
    if not is_bot_request(user_agent):
        return None

    # CrawlerDetect match (may return a list or string)
    if _crawler_detect.isCrawler(user_agent):
        matches = _crawler_detect.getMatches()
        if matches:
            return str(matches)

    # Fall back to first regex match
    bot_patterns = _load_bot_user_agents()
    for pattern in bot_patterns:
        if re.search(pattern, user_agent, re.IGNORECASE):
            return pattern

    return None


def should_block_bot(
    method: str,
    user_agent: str,
    url_data: UrlCacheData,
    schema: str,
) -> bool:
    """Pre-emit redirect-blocking decision for the hot path.

    Only v1/emoji URLs with ``block_bots`` return 403 instead of the
    redirect (v2 never blocks redirects — its bot handling is
    analytics-skip inside the click pipeline). HEAD/OPTIONS are exempt
    from tracking and therefore from the decision; an empty User-Agent
    cannot be classified and falls through to the pipeline's
    ValidationError path — both matching the pre-sink inline behavior.
    """
    return (
        method not in ("HEAD", "OPTIONS")
        and bool(user_agent)
        and url_data.block_bots
        and schema != SchemaVersion.V2
        and is_bot_request(user_agent)
    )
