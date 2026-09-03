"""Unit tests for DiscordOpsNotifier — delivery semantics, channel
routing, and embed formatting (owned here, not by the calling services)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.ops_notify import DiscordOpsNotifier

_CONTACT_URL = "https://discord.com/api/webhooks/123/contact"
_REPORT_URL = "https://discord.com/api/webhooks/123/report"


def _make(contact_url=_CONTACT_URL, report_url=_REPORT_URL, status_code=204):
    http = MagicMock()
    resp = MagicMock(status_code=status_code, text="Bad Request")
    http.post = AsyncMock(return_value=resp)
    return DiscordOpsNotifier(contact_url, report_url, http), http


class TestDelivery:
    async def test_returns_true_on_204(self):
        notifier, _ = _make(status_code=204)
        assert await notifier.contact_message("a@b.c", "hi") is True

    async def test_returns_true_on_200(self):
        notifier, _ = _make(status_code=200)
        assert await notifier.contact_message("a@b.c", "hi") is True

    async def test_returns_false_on_error_status(self):
        notifier, _ = _make(status_code=400)
        assert await notifier.contact_message("a@b.c", "hi") is False

    async def test_returns_false_when_channel_unconfigured(self):
        notifier, http = _make(contact_url="")
        assert await notifier.contact_message("a@b.c", "hi") is False
        http.post.assert_not_awaited()

    async def test_returns_false_on_exception(self):
        notifier, http = _make()
        http.post = AsyncMock(side_effect=Exception("network error"))
        assert await notifier.contact_message("a@b.c", "hi") is False


class TestChannelRouting:
    async def test_contact_message_goes_to_contact_channel(self):
        notifier, http = _make()
        await notifier.contact_message("a@b.c", "hi")
        assert http.post.call_args[0][0] == _CONTACT_URL

    async def test_url_report_goes_to_report_channel(self):
        notifier, http = _make()
        await notifier.url_report("abc123", "spam", "1.2.3.4", "https://spoo.me/")
        assert http.post.call_args[0][0] == _REPORT_URL

    async def test_unconfigured_report_channel_does_not_leak_to_contact(self):
        notifier, http = _make(report_url="")
        assert (
            await notifier.url_report("abc123", "spam", "1.2.3.4", "https://spoo.me/")
            is False
        )
        http.post.assert_not_awaited()


class TestContactEmbed:
    async def test_embed_contains_email_and_message(self):
        notifier, http = _make()
        await notifier.contact_message("user@example.com", "My message")
        embed = http.post.call_args.kwargs["json"]["embeds"][0]
        field_names = [f["name"] for f in embed["fields"]]
        assert "Email" in field_names
        assert "Message" in field_names
        assert embed["fields"][0]["value"] == "```user@example.com```"
        assert embed["fields"][1]["value"] == "```My message```"

    async def test_embed_title_is_new_contact_message(self):
        notifier, http = _make()
        await notifier.contact_message("user@example.com", "Hello")
        embed = http.post.call_args.kwargs["json"]["embeds"][0]
        assert "Contact" in embed["title"]
        assert embed["footer"]["text"] == "spoo-me"


class TestUrlReportEmbed:
    async def test_embed_contains_short_code_reason_ip(self):
        notifier, http = _make()
        await notifier.url_report("abc123", "phishing", "1.2.3.4", "https://spoo.me/")
        embed = http.post.call_args.kwargs["json"]["embeds"][0]
        field_names = [f["name"] for f in embed["fields"]]
        assert "Short Code" in field_names
        assert "Reason" in field_names
        assert "IP Address" in field_names

    async def test_embed_title_contains_short_code(self):
        notifier, http = _make()
        await notifier.url_report("abc123", "spam", "1.2.3.4", "https://spoo.me/")
        embed = http.post.call_args.kwargs["json"]["embeds"][0]
        assert "abc123" in embed["title"]

    async def test_embed_url_points_to_stats_page(self):
        notifier, http = _make()
        await notifier.url_report("abc123", "spam", "1.2.3.4", "https://spoo.me/")
        embed = http.post.call_args.kwargs["json"]["embeds"][0]
        assert embed["url"] == "https://spoo.me/stats/abc123"


class TestSendEmbed:
    """The generic embed owns every Discord specific: routing, image as a
    multipart attachment, the 1024-char field cap, and never sending a
    webhook credential or a screenshot in the clear or through a redirect."""

    @staticmethod
    def _fields():
        return [
            {"name": "A", "value": "```one```"},
            {"name": "B", "value": "```two```"},
        ]

    async def test_plain_embed_is_json_to_the_named_channel(self):
        notifier, http = _make()
        await notifier.send_embed(
            channel="report", title="T", color=1, fields=self._fields()
        )
        assert http.post.call_args[0][0] == _REPORT_URL
        kw = http.post.call_args.kwargs
        assert kw["follow_redirects"] is False
        assert "files" not in kw and "data" not in kw
        embed = kw["json"]["embeds"][0]
        assert embed["title"] == "T" and "image" not in embed
        assert [f["name"] for f in embed["fields"]] == ["A", "B"]

    async def test_image_rides_as_multipart_attachment(self):
        import json

        notifier, http = _make()
        await notifier.send_embed(
            channel="report", title="T", color=1, fields=self._fields(), image=b"webp!"
        )
        kw = http.post.call_args.kwargs
        assert "json" not in kw
        assert kw["follow_redirects"] is False
        assert kw["files"]["files[0]"] == ("evidence.webp", b"webp!", "image/webp")
        payload = json.loads(kw["data"]["payload_json"])
        assert payload["embeds"][0]["image"] == {"url": "attachment://evidence.webp"}

    async def test_contact_channel_routes_to_contact_url(self):
        notifier, http = _make()
        await notifier.send_embed(
            channel="contact", title="T", color=1, fields=self._fields()
        )
        assert http.post.call_args[0][0] == _CONTACT_URL

    async def test_http_webhook_is_refused_not_sent(self):
        """A webhook URL is a bearer credential."""
        notifier, http = _make(report_url="http://discord.com/api/webhooks/1/x")
        assert (
            await notifier.send_embed(
                channel="report", title="T", color=1, fields=self._fields(), image=b"s"
            )
            is False
        )
        http.post.assert_not_awaited()

    async def test_over_long_field_is_clipped_keeping_fences(self):
        """Discord rejects the whole message over 1024 chars per field. The
        verdict keeps the full text; only the display clips."""
        notifier, http = _make()
        long = "```" + "x" * 2000 + "```"
        await notifier.send_embed(
            channel="report",
            title="T",
            color=1,
            fields=[{"name": "Scope", "value": long}],
        )
        value = http.post.call_args.kwargs["json"]["embeds"][0]["fields"][0]["value"]
        assert len(value) <= 1024
        assert value.startswith("```") and value.endswith("…```")

    async def test_unknown_channel_is_refused_not_misrouted(self):
        """A typo like "reports" must not deliver a block embed to the
        contact channel with no signal."""
        notifier, http = _make()
        assert (
            await notifier.send_embed(
                channel="reports", title="T", color=1, fields=self._fields()
            )
            is False
        )
        http.post.assert_not_awaited()

    async def test_short_field_is_untouched(self):
        from infrastructure.ops_notify import _bound_field

        assert _bound_field("```ok```") == "```ok```"


@pytest.mark.asyncio
async def test_attachment_name_and_type_follow_the_image_bytes():
    """The probe browser sends JPEG; the old code named everything .webp,
    which Discord then refused to render inline."""
    notifier, http = _make()
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    await notifier.send_embed(
        channel="report",
        title="T",
        color=1,
        fields=[{"name": "Host", "value": "x"}],
        image=jpeg,
    )
    kw = http.post.call_args.kwargs
    assert kw["files"]["files[0]"] == ("evidence.jpg", jpeg, "image/jpeg")
    payload = json.loads(kw["data"]["payload_json"])
    assert payload["embeds"][0]["image"] == {"url": "attachment://evidence.jpg"}
