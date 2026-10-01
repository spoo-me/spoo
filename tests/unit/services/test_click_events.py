"""Tests for the ClickEvent DTO and its stream wire format."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.infrastructure.cache.url_cache import (
    UrlCacheData,
)
from app.services.click.events import (
    EVENT_TYPE_CLICK,
    STREAM_FIELD_DATA,
    STREAM_FIELD_TYPE,
    STREAM_FIELD_VERSION,
    click_event_from_payload,
    from_stream_fields,
    to_stream_fields,
)
from tests.factories import make_click_event, make_url_cache

# Local aliases — shape lives in tests/factories.py
make_url_data = make_url_cache
make_event = make_click_event


class TestClickEvent:
    def test_event_is_frozen(self):
        event = make_event()
        with pytest.raises(PydanticValidationError):
            event.redirect_ms = 99  # type: ignore[misc]

    def test_enqueued_at_defaults_to_now_utc(self):
        event = make_event()
        assert event.enqueued_at.tzinfo is not None

    def test_url_snapshot_preserves_nested_model(self):
        event = make_event(url=make_url_data(domain="", schema_version="v1"))
        assert isinstance(event.url, UrlCacheData)
        assert event.url.schema_version == "v1"


class TestWireFormat:
    def test_round_trip_through_stream_fields(self):
        event = make_event()
        fields = to_stream_fields(event)

        assert fields[STREAM_FIELD_TYPE] == EVENT_TYPE_CLICK
        assert fields[STREAM_FIELD_VERSION] == "1"
        assert STREAM_FIELD_DATA in fields

        restored = from_stream_fields(fields)
        assert restored == event

    def test_wire_fields_are_flat_strings(self):
        """XADD requires flat str/bytes field values."""
        fields = to_stream_fields(make_event())
        for key, value in fields.items():
            assert isinstance(key, str)
            assert isinstance(value, str)

    def test_from_stream_fields_returns_none_on_malformed_json(self):
        assert from_stream_fields({STREAM_FIELD_DATA: "{not json"}) is None

    def test_from_stream_fields_returns_none_on_missing_data_field(self):
        assert from_stream_fields({"other": "x"}) is None

    def test_from_stream_fields_returns_none_on_schema_mismatch(self):
        assert from_stream_fields({STREAM_FIELD_DATA: '{"short_code": "abc"}'}) is None


class TestPayloadDecode:
    """The FastStream path: handlers receive the decoded __data__ payload."""

    def test_decodes_dict_payload(self):
        event = make_event()
        payload = event.model_dump(mode="json")
        restored = click_event_from_payload(payload)
        assert restored == event

    def test_returns_none_on_non_dict_payload(self):
        assert click_event_from_payload("raw string") is None
        assert click_event_from_payload(None) is None
        assert click_event_from_payload([1, 2]) is None

    def test_returns_none_on_invalid_fields(self):
        assert click_event_from_payload({"short_code": "abc"}) is None

    def test_tolerates_unknown_extra_fields(self):
        """Forward compat: a newer producer may add fields."""
        payload = make_event().model_dump(mode="json")
        payload["future_field"] = "whatever"
        restored = click_event_from_payload(payload)
        assert restored is not None
        assert restored.short_code == "abc"


class TestPasswordHashSanitization:
    """The privacy invariant is structural: no producer can leak a hash."""

    def test_password_hash_stripped_on_construction(self):
        event = make_event(url=make_url_data(password_hash="plaintext-v1-password"))
        assert event.url.password_hash is None

    def test_hashless_url_passes_through_unchanged(self):
        url = make_url_data(password_hash=None)
        assert make_event(url=url).url is url


class TestGeoDecisionFields:
    def test_defaults_when_not_stamped(self):
        event = make_event()
        assert event.resolved_country is None
        assert event.geo_matched is False

    def test_stamped_values_round_trip_through_stream(self):
        event = make_event(resolved_country="IN", geo_matched=True)
        restored = from_stream_fields(to_stream_fields(event))
        assert restored.resolved_country == "IN"
        assert restored.geo_matched is True

    def test_pre_geo_stream_payload_still_parses(self):
        """Payloads enqueued before the geo fields existed must decode —
        at-least-once delivery means old entries can be claimed post-deploy."""
        import json as _json

        fields = to_stream_fields(make_event())
        data = _json.loads(fields[STREAM_FIELD_DATA])
        data.pop("resolved_country", None)
        data.pop("geo_matched", None)
        fields[STREAM_FIELD_DATA] = _json.dumps(data)

        restored = from_stream_fields(fields)
        assert restored.resolved_country is None
        assert restored.geo_matched is False


class TestVariantIndexField:
    def test_defaults_to_none(self):
        assert make_event().variant_index is None

    def test_round_trips_through_stream(self):
        restored = from_stream_fields(to_stream_fields(make_event(variant_index=1)))
        assert restored.variant_index == 1

    def test_pre_variant_stream_payload_still_parses(self):
        import json as _json

        fields = to_stream_fields(make_event())
        data = _json.loads(fields[STREAM_FIELD_DATA])
        data.pop("variant_index", None)
        fields[STREAM_FIELD_DATA] = _json.dumps(data)
        assert from_stream_fields(fields).variant_index is None


class TestUtmSanitization:
    """UTM values are visitor-controlled input — the bound is structural,
    enforced at event construction like the password-hash strip."""

    def test_defaults_when_not_stamped(self):
        event = make_event()
        assert event.utm_source is None
        assert event.utm_medium is None
        assert event.utm_campaign is None

    def test_stamped_values_round_trip_through_stream(self):
        event = make_event(
            utm_source="newsletter", utm_medium="email", utm_campaign="launch"
        )
        restored = from_stream_fields(to_stream_fields(event))
        assert restored.utm_source == "newsletter"
        assert restored.utm_medium == "email"
        assert restored.utm_campaign == "launch"

    def test_control_characters_stripped(self):
        event = make_event(utm_source="news\x00let\x1fter\x7f")
        assert event.utm_source == "newsletter"

    def test_value_truncated_to_bound(self):
        event = make_event(utm_campaign="x" * 500)
        assert len(event.utm_campaign) == 100

    def test_whitespace_only_becomes_none(self):
        event = make_event(utm_medium="   ")
        assert event.utm_medium is None

    def test_empty_string_becomes_none(self):
        event = make_event(utm_source="")
        assert event.utm_source is None

    def test_pre_utm_stream_payload_still_parses(self):
        import json as _json

        fields = to_stream_fields(make_event())
        data = _json.loads(fields[STREAM_FIELD_DATA])
        for key in ("utm_source", "utm_medium", "utm_campaign"):
            data.pop(key, None)
        fields[STREAM_FIELD_DATA] = _json.dumps(data)

        restored = from_stream_fields(fields)
        assert restored.utm_source is None
        assert restored.utm_medium is None
        assert restored.utm_campaign is None
