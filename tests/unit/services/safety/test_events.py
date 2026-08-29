"""Unit tests for the safety stream wire contract."""

from __future__ import annotations

from services.safety.events import (
    STREAM_FIELD_DATA,
    SafetyAnalyzeEvent,
    safety_event_from_payload,
    to_stream_fields,
)


class TestWireRoundtrip:
    def test_encode_decode(self):
        event = SafetyAnalyzeEvent(
            url="https://evil.com/kit",
            host="evil.com",
            registrable_domain="evil.com",
            trigger="report",
            context={"reasons": ["phishing"], "report_count": 2},
        )
        fields = to_stream_fields(event)
        assert fields["type"] == "safety.analyze"
        decoded = safety_event_from_payload(
            {STREAM_FIELD_DATA: fields[STREAM_FIELD_DATA]}
        )
        assert decoded == event

    def test_decodes_bare_json_string(self):
        event = SafetyAnalyzeEvent(url="https://a.com/x", host="a.com")
        decoded = safety_event_from_payload(event.model_dump_json())
        assert decoded is not None
        assert decoded.host == "a.com"

    def test_decodes_plain_dict(self):
        decoded = safety_event_from_payload({"url": "https://a.com/x", "host": "a.com"})
        assert decoded is not None
        assert decoded.trigger == "report"  # default


class TestDropDontPoison:
    def test_malformed_json_returns_none(self):
        assert safety_event_from_payload("{not json") is None

    def test_missing_required_fields_returns_none(self):
        assert safety_event_from_payload({"url": "https://a.com"}) is None

    def test_unsupported_type_returns_none(self):
        assert safety_event_from_payload(12345) is None
