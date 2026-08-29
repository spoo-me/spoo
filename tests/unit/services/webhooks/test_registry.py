"""Event catalog — wildcard expansion, validation, sample/model lockstep."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from errors import ValidationError
from services.webhooks.registry import (
    EVENT_REGISTRY,
    TEST_EVENT_SPEC,
    expand,
    valid_patterns,
    validate_patterns,
    validate_payload,
)

EXPECTED_EVENTS = {
    "link.created",
    "link.updated",
    "link.deleted",
    "link.clicked",
    "link.expired",
    "link.blocked",
}


class TestCatalog:
    def test_the_ten_events(self):
        assert set(EVENT_REGISTRY) == EXPECTED_EVENTS

    def test_every_sample_validates_against_its_payload_model(self):
        """Samples double as docs AND test-send fixtures — they can never
        drift from the payload models because this test pins them."""
        for spec in [*EVENT_REGISTRY.values(), TEST_EVENT_SPEC]:
            spec.payload_model.model_validate(spec.sample())

    def test_click_sample_has_no_ip_shaped_fields(self):
        sample = EVENT_REGISTRY["link.clicked"].sample()
        assert "ip" not in sample
        assert "client_ip" not in sample


class TestExpansion:
    def test_star_is_everything(self):
        assert expand(["*"]) == frozenset(EVENT_REGISTRY)

    def test_category_wildcard(self):
        assert expand(["link.*"]) == {
            name for name, spec in EVENT_REGISTRY.items() if spec.category == "link"
        }

    def test_concrete_names(self):
        assert expand(["link.clicked", "link.expired"]) == {
            "link.clicked",
            "link.expired",
        }

    def test_unknown_patterns_expand_to_nothing(self):
        assert expand(["nope.*", "nope"]) == frozenset()


class TestValidation:
    def test_valid_patterns_include_wildcards(self):
        patterns = valid_patterns()
        assert "*" in patterns
        assert "link.*" in patterns
        assert "link.clicked" in patterns

    def test_typo_gets_a_suggestion(self):
        with pytest.raises(ValidationError, match=r"link\.clicked"):
            validate_patterns(["link.clickd"])

    def test_unknown_event_rejected(self):
        with pytest.raises(ValidationError):
            validate_patterns(["payments.settled"])

    def test_payload_validation_rejects_missing_required(self):
        with pytest.raises(PydanticValidationError):
            validate_payload("link.created", {})

    def test_payload_validation_allows_additive_fields(self):
        sample = EVENT_REGISTRY["link.clicked"].sample()
        sample["brand_new_field"] = "future"
        validate_payload("link.clicked", sample)
