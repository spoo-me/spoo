"""Discord and Slack flavors — full-shape pins per event type, receiver
size limits, escaping, and the fallback that keeps future event types from
terminal-failing flavored endpoints. Raw stays pinned via the executor
tests; these cover the lossy renderings."""

from __future__ import annotations

import json
from typing import Any

from app.schemas.enums.webhook import WebhookFlavor
from app.services.webhooks.registry import EVENT_REGISTRY, TEST_EVENT_SPEC
from app.services.webhooks.renderers import default_renderers
from app.services.webhooks.renderers.discord import DiscordRenderer
from app.services.webhooks.renderers.slack import SlackRenderer

_TS = "2026-07-24T14:32:00+00:00"


def _discord(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = DiscordRenderer().render("evt_x", event_type, _TS, payload)
    return json.loads(body)


def _slack(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = SlackRenderer().render("evt_x", event_type, _TS, payload)
    return json.loads(body)


def _clicked_payload(**overrides: Any) -> dict[str, Any]:
    payload = EVENT_REGISTRY["link.clicked"].sample()
    payload.update(overrides)
    return payload


class TestRegistry:
    def test_every_flavor_has_a_renderer(self):
        # Enum and registry must never drift: a flavor an endpoint can be
        # created with must be renderable, or deliveries terminal-fail.
        assert set(default_renderers()) == {f.value for f in WebhookFlavor}

    def test_renderers_expose_matching_flavor_attribute(self):
        for key, renderer in default_renderers().items():
            assert renderer.flavor == key


class TestDiscord:
    """Components V2: one accent container, heading, divider, substance,
    small-text localized footer. Never embeds, never pings."""

    def _container(self, doc):
        assert doc["flags"] == 32768
        assert doc["allowed_mentions"] == {"parse": []}
        assert "embeds" not in doc and "content" not in doc
        (container,) = doc["components"]
        assert container["type"] == 17
        return container

    def _texts(self, container):
        return [c["content"] for c in container["components"] if c["type"] == 10]

    def test_clicked_message_shape(self):
        doc = _discord("link.clicked", _clicked_payload())
        assert doc["username"] == "spoo.me"
        assert doc["avatar_url"].startswith("https://spoo.me/")
        container = self._container(doc)
        texts = self._texts(container)
        assert texts[0] == (
            "### Click  [spoo.me/summer-drop](https://spoo.me/summer-drop)"
        )
        body = texts[1]
        assert "🇮🇳 **IN**  Mumbai" in body
        assert "Chrome on Android, mobile" in body
        assert "from **x.com**  (newsletter / email)" in body
        footer = texts[-1]
        assert footer.startswith("-# click 4,102")
        assert "<t:" in footer and footer.rstrip().endswith(":R>")
        # A divider separates the heading from the substance.
        assert any(c["type"] == 14 and c["divider"] for c in container["components"])
        assert "·" not in json.dumps(doc, ensure_ascii=False)

    def test_sparse_click_is_never_an_empty_card(self):
        doc = _discord(
            "link.clicked",
            _clicked_payload(
                city=None,
                country="unknown",
                referrer="(none)",
                utm=None,
                total_clicks=0,
            ),
        )
        texts = self._texts(self._container(doc))
        assert "Chrome on Android, mobile" in texts[1]
        assert "direct visit" in texts[1]
        assert texts[-1].startswith("-# click 0")

    def test_clicked_bot_line(self):
        doc = _discord(
            "link.clicked", _clicked_payload(is_bot=True, bot_name="GoogleBot")
        )
        texts = self._texts(self._container(doc))
        assert "bot  **GoogleBot**" in texts[1]

    def test_dropped_notice_rides_the_footer(self):
        doc = _discord("link.clicked", _clicked_payload(dropped_since_last=3))
        footer = self._texts(self._container(doc))[-1]
        assert "-# 3 earlier deliveries dropped" in footer

    def test_updated_renders_a_diff_block(self):
        payload = EVENT_REGISTRY["link.updated"].sample()
        payload["changes"]["max_clicks"] = {"old": 5000, "new": 10000}
        texts = self._texts(self._container(_discord("link.updated", payload)))
        diff = next(t for t in texts if t.startswith("```diff"))
        assert "- long_url: https://example.com/old" in diff
        assert "+ long_url: https://example.com/campaign" in diff
        assert "- max_clicks: 5000" in diff
        assert "+ max_clicks: 10000" in diff

    def test_diff_values_cannot_break_out_of_the_block(self):
        payload = {
            "link": {"alias": "a", "domain": "spoo.me"},
            "changes": {"long_url": {"old": "x", "new": "```@everyone"}},
        }
        texts = self._texts(self._container(_discord("link.updated", payload)))
        diff = next(t for t in texts if t.startswith("```diff"))
        assert diff.count("```") == 2  # only the fence itself

    def test_created_states_the_full_configuration(self):
        payload = {"link": {**EVENT_REGISTRY["link.created"].sample()["link"]}}
        payload["link"]["expires_at"] = None
        payload["link"]["max_clicks"] = None
        container = self._container(_discord("link.created", payload))
        texts = self._texts(container)
        # Destination rides as a small subtitle under the heading.
        assert texts[1] == "-# https://example.com/campaign"
        facts = texts[2]
        assert "**Expires**  never" in facts
        assert "**Max clicks**  unlimited" in facts
        assert "**Password**  none" in facts
        assert "**Bots**  allowed" in facts
        assert "**Meta tags**  default" in facts
        assert "**Geo targeting**  none" in facts
        assert container["accent_color"] == 0x43B581

    def test_deleted_heading_is_not_a_link(self):
        texts = self._texts(
            self._container(
                _discord("link.deleted", EVENT_REGISTRY["link.deleted"].sample())
            )
        )
        assert texts[0] == "### Link deleted  spoo.me/summer-drop"
        assert "**Lifetime clicks**  4,102" in texts[2]
        assert "**Age**" in texts[2]

    def test_test_event_renders_its_message(self):
        texts = self._texts(
            self._container(_discord("webhook.test", TEST_EVENT_SPEC.sample()))
        )
        assert "If you can read this, your endpoint works." in texts

    def test_unknown_event_type_falls_back_to_json(self):
        # A future registry addition must degrade, never terminal-fail.
        texts = self._texts(
            self._container(
                _discord("link.blocked", {"link_id": "x", "reason": "phishing"})
            )
        )
        assert texts[0] == "### link.blocked"
        code = next(t for t in texts if t.startswith("```json"))
        assert '"reason":"phishing"' in code

    def test_text_budget_holds_under_pathological_changes(self):
        payload = {
            "link": {"alias": "a" * 500, "domain": "spoo.me"},
            "changes": {
                f"field_{i}": {"old": "x" * 900, "new": "y" * 900} for i in range(10)
            },
        }
        container = self._container(_discord("link.updated", payload))
        total = sum(len(t) for t in self._texts(container))
        assert total <= 3_500
        assert len(container["components"]) <= 40

    def test_delivery_url_query_declared(self):
        assert DiscordRenderer.url_query == {"with_components": "true"}


class TestSlack:
    def test_clicked_is_title_fields_context(self):
        doc = _slack("link.clicked", _clicked_payload())
        assert doc["text"] == "Click: spoo.me/summer-drop"
        first, last = doc["blocks"][0], doc["blocks"][-1]
        assert first["type"] == "section"
        assert first["text"]["text"].startswith(
            "*<https://spoo.me/summer-drop|Click: spoo.me/summer-drop>*"
        )
        fields = [f["text"] for f in doc["blocks"][1]["fields"]]
        assert "*Country*\n🇮🇳 IN" in fields
        assert "*From*\nx.com" in fields
        assert last["type"] == "context"
        assert last["elements"][0]["text"] == f"spoo.me · {_TS}"

    def test_updated_renders_old_to_new_pairs(self):
        doc = _slack("link.updated", EVENT_REGISTRY["link.updated"].sample())
        fields = doc["blocks"][1]["fields"]
        assert fields[0]["text"] == (
            "*long_url*\nhttps://example.com/old → https://example.com/campaign"
        )

    def test_field_sections_chunk_at_ten(self):
        payload = {
            "link": {"alias": "a", "domain": "spoo.me"},
            "changes": {f"f{i}": {"old": i, "new": i + 1} for i in range(12)},
        }
        doc = _slack("link.updated", payload)
        field_sections = [b for b in doc["blocks"] if "fields" in b]
        assert [len(s["fields"]) for s in field_sections] == [10, 2]

    def test_mrkdwn_control_characters_escaped(self):
        payload = _clicked_payload(city="<Berlin & Brandenburg>")
        doc = _slack("link.clicked", payload)
        fields = " ".join(f["text"] for f in doc["blocks"][1]["fields"])
        assert "&lt;Berlin &amp; Brandenburg&gt;" in fields
        assert "<Berlin" not in fields

    def test_dropped_notice_rides_the_context_line(self):
        doc = _slack("link.clicked", _clicked_payload(dropped_since_last=1))
        assert doc["blocks"][-1]["elements"][0]["text"] == (
            f"spoo.me · {_TS} · 1 earlier delivery dropped"
        )

    def test_unknown_event_type_falls_back_to_json(self):
        doc = _slack("link.blocked", {"link_id": "x", "reason": "phishing"})
        assert doc["text"] == "link.blocked"
        code = doc["blocks"][1]["text"]["text"]
        assert code.startswith("```")
        assert "phishing" in code

    def test_output_stays_under_the_engine_payload_cap(self):
        payload = {
            "link": {"alias": "a" * 500, "domain": "spoo.me"},
            "changes": {
                f"field_{i}": {"old": "x" * 900, "new": "y" * 900} for i in range(10)
            },
        }
        body = SlackRenderer().render("evt_x", "link.updated", _TS, payload)
        assert len(body.encode()) < 20_480
        assert len(json.loads(body)["blocks"]) <= 50


class TestAllEventsBothFlavors:
    def test_every_registered_sample_renders_valid_json(self):
        renderers = default_renderers()
        specs = [*EVENT_REGISTRY.values(), TEST_EVENT_SPEC]
        for spec in specs:
            for flavor in ("discord", "slack"):
                body = renderers[flavor].render("evt_x", spec.name, _TS, spec.sample())
                parsed = json.loads(body)
                assert parsed  # non-empty, valid JSON
                assert len(body.encode()) < 20_480


class TestDiscordInjection:
    """Visitor-controlled payload values must never render as live
    Discord markdown in the subscriber's channel."""

    def _body_text(self, doc):
        return "\n".join(
            c["content"] for c in doc["components"][0]["components"] if c["type"] == 10
        )

    def test_masked_link_in_referrer_is_neutralized(self):
        doc = _discord(
            "link.clicked", _clicked_payload(referrer="[x](https://evil.phish)")
        )
        text = self._body_text(doc)
        assert "[x](https://evil.phish)" not in text
        assert "\\[x\\]" in text

    def test_markdown_in_dimensions_is_neutralized(self):
        doc = _discord(
            "link.clicked",
            _clicked_payload(browser="**bold**", city="`code`", os="||spoiler||"),
        )
        text = self._body_text(doc)
        assert "**bold**" not in text
        assert "`code`" not in text
        assert "||spoiler||" not in text

    def test_lifecycle_values_are_neutralized(self):
        payload = {
            "link": {
                "alias": "a",
                "domain": "spoo.me",
                "long_url": "https://example.com/a(b)[c]",
                "short_url": "https://spoo.me/a",
            }
        }
        doc = _discord("link.created", payload)
        text = self._body_text(doc)
        assert "(b)[c]" not in text  # subtitle rides escaped

    def test_link_target_parens_cannot_close_the_masked_link(self):
        payload = _clicked_payload()
        payload["short_url"] = "https://spoo.me/a)b"
        doc = _discord("link.clicked", payload)
        heading = doc["components"][0]["components"][0]["content"]
        assert "](https://spoo.me/a%29b)" in heading
