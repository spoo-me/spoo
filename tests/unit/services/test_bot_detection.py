"""Tests for bot detection — the pre-emit blocking decision in particular."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.click.bot_detection import (
    is_bot_request,
    should_block_bot,
    wants_preview,
)
from tests.factories import make_url_cache

BOT_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class TestIsBotRequest:
    def test_known_bot(self):
        assert is_bot_request(BOT_UA) is True

    def test_browser(self):
        assert is_bot_request(BROWSER_UA) is False

    def test_empty_ua_is_unclassifiable(self):
        assert is_bot_request("") is False


class TestShouldBlockBot:
    def test_v1_bot_on_blocking_url_is_blocked(self):
        url = make_url_cache(schema_version="v1", block_bots=True)
        assert should_block_bot("GET", BOT_UA, url, "v1") is True

    def test_emoji_bot_on_blocking_url_is_blocked(self):
        url = make_url_cache(schema_version="v1", block_bots=True)
        assert should_block_bot("GET", BOT_UA, url, "emoji") is True

    def test_v2_never_blocks_the_redirect(self):
        """v2 bot handling is analytics-skip in the pipeline, not a 403."""
        url = make_url_cache(schema_version="v2", block_bots=True)
        assert should_block_bot("GET", BOT_UA, url, "v2") is False

    def test_block_bots_off_never_blocks(self):
        url = make_url_cache(schema_version="v1", block_bots=False)
        assert should_block_bot("GET", BOT_UA, url, "v1") is False

    def test_human_ua_never_blocks(self):
        url = make_url_cache(schema_version="v1", block_bots=True)
        assert should_block_bot("GET", BROWSER_UA, url, "v1") is False

    def test_empty_ua_falls_through_to_pipeline(self):
        """Unclassifiable UA → pipeline's ValidationError path decides."""
        url = make_url_cache(schema_version="v1", block_bots=True)
        assert should_block_bot("GET", "", url, "v1") is False

    def test_head_and_options_are_exempt(self):
        url = make_url_cache(schema_version="v1", block_bots=True)
        assert should_block_bot("HEAD", BOT_UA, url, "v1") is False
        assert should_block_bot("OPTIONS", BOT_UA, url, "v1") is False


# ── wants_preview (custom meta-tags serving decision) ─────────────────────────

PREVIEW_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "facebookexternalhit/1.1 Facebot Twitterbot/1.0",  # iMessage spoof
    "WhatsApp/2.23.20.0 A",  # also Signal + Primal
    "TelegramBot (like TwitterBot)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Twitterbot/1.0",
    "LinkedInBot/1.0 (compatible; Mozilla/5.0)",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) SkypeUriPreview Preview/0.5",
    "facebookexternalhit/1.1;kakaotalk-scrap/1.0;",
    "facebookexternalhit/1.1;line-poker/1.0",
    "Mozilla/5.0 (compatible; Pinterestbot/1.0; +https://www.pinterest.com/bot.html)",
    "Synapse (bot; +https://github.com/matrix-org/synapse)",
    "Iframely/1.3.1 (+https://iframely.com/docs/about) Atlassian",
    "Mastodon/4.2.1 (http.rb/5.1.1; +https://mastodon.social/) Bot",
    "MetadataScraper",  # heymeta.com — discovered via live tunnel recon 2026-07-09
]

NON_PREVIEW_UAS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
    "Mozilla/5.0 (compatible; ClaudeBot/1.0)",
    "Mozilla/5.0 (compatible; PerplexityBot/1.0)",
    "Mozilla/5.0 (compatible; Applebot/0.1; +http://www.apple.com/go/applebot)",
    "curl/8.4.0",  # generic tools follow the 302 — today's behavior
    "python-requests/2.31",
    BROWSER_UA,
    "",
]


class TestWantsPreview:
    @pytest.mark.parametrize("ua", PREVIEW_UAS)
    def test_preview_crawlers_get_page(self, ua):
        assert wants_preview("GET", ua) is True

    @pytest.mark.parametrize("ua", NON_PREVIEW_UAS)
    def test_everyone_else_gets_302(self, ua):
        assert wants_preview("GET", ua) is False

    def test_head_is_preview(self):
        assert wants_preview("HEAD", BROWSER_UA) is True

    def test_bot_param_forces_preview(self):
        assert wants_preview("GET", BROWSER_UA, bot_param=True) is True

    def test_inapp_browsers_are_humans(self):
        # LINE in-app browser (real human traffic) — the trap that burned
        # Dub's generic matching. LINE's preview fetcher carries line-poker.
        assert (
            wants_preview(
                "GET",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5) AppleWebKit/605.1.15 Line/13.5.0",
            )
            is False
        )
        # Pinterest mobile app (not Pinterestbot)
        assert (
            wants_preview("GET", "Mozilla/5.0 (iPhone) Pinterest for iOS/12.1") is False
        )

    def test_case_sensitive_tokens(self):
        assert wants_preview("GET", "Bluesky Cardyb/1.1") is True
        assert (
            wants_preview("GET", "Mozilla/5.0 https://bluesky.example/x Chrome/126.0")
            is False
        )

    def test_tokens_match_edge_copy(self):
        root = Path(__file__).resolve().parents[3]
        a = (root / "app" / "data" / "preview_bots.json").read_bytes()
        b = (
            root / "edge" / "spoo-edge-cache" / "contract" / "preview_bots.json"
        ).read_bytes()
        assert a == b
