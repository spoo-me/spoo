"""Pure resolution: override > plan default, typed by the catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from schemas.models.entitlement_override import EntitlementOverrideDoc, OverrideKind
from services.entitlements.resolver import ANONYMOUS, Resolved, for_plan, resolve
from services.features.catalog import FEATURES, UNLIMITED, Plan, plan_defaults

UID = ObjectId()


def _override(key: str, value, **extra) -> EntitlementOverrideDoc:
    return EntitlementOverrideDoc(
        user_id=UID,
        key=key,
        value=value,
        kind=OverrideKind.COMP,
        reason="test",
        granted_by="tests",
        **extra,
    )


def test_no_overrides_is_plan_defaults():
    assert resolve(Plan.FREE) == plan_defaults(Plan.FREE)
    assert resolve(Plan.PRO) == plan_defaults(Plan.PRO)


def test_override_beats_plan_default_for_bool_and_int():
    values = resolve(
        Plan.FREE,
        [_override("geo_targeting", True), _override("custom_domains_max", 3)],
    )
    assert values["geo_targeting"] is True
    assert values["custom_domains_max"] == 3
    # Everything else is untouched.
    assert values["ab_variants"] is False
    assert values["webhook_endpoints_max"] == 1


def test_override_can_lower_a_pro_value():
    values = resolve(Plan.PRO, [_override("api_rate_multiplier", 1)])
    assert values["api_rate_multiplier"] == 1


def test_override_values_are_coerced_to_the_catalog_kind():
    values = resolve(
        Plan.FREE,
        [_override("geo_targeting", 1), _override("bulk_batch_max", True)],
    )
    assert values["geo_targeting"] is True
    assert values["bulk_batch_max"] == 1


def test_unknown_override_key_is_ignored():
    values = resolve(Plan.FREE, [_override("not_a_feature", True)])
    assert "not_a_feature" not in values
    assert values == plan_defaults(Plan.FREE)


def test_anonymous_constant_is_the_anonymous_plan():
    assert ANONYMOUS.plan is Plan.ANONYMOUS
    assert ANONYMOUS.version == 0
    assert ANONYMOUS.values == plan_defaults(Plan.ANONYMOUS)


def test_resolved_helpers():
    r = for_plan(Plan.PRO)
    assert r.has("geo_targeting") is True
    assert r.limit("custom_domains_max") == 5
    assert r.within_limit("custom_domains_max", 4) is True
    assert r.within_limit("custom_domains_max", 5) is False
    selfhost = for_plan(Plan.SELFHOST)
    assert selfhost.is_unlimited("custom_domains_max")
    assert selfhost.within_limit("custom_domains_max", 10_000) is True
    assert selfhost.limit("custom_domains_max") == UNLIMITED


def test_limit_of_a_bool_key_is_zero_not_one():
    assert for_plan(Plan.PRO).limit("geo_targeting") == 0


def test_degraded_is_never_serialised():
    r = for_plan(Plan.FREE).model_copy(update={"degraded": True})
    assert "degraded" not in r.model_dump()
    assert Resolved.model_validate_json(r.model_dump_json()).degraded is False


def test_round_trips_through_json_with_status_and_until():
    r = Resolved(
        plan=Plan.PRO,
        status="grace",
        until=datetime(2026, 10, 1, tzinfo=timezone.utc),
        founding=True,
        values=plan_defaults(Plan.PRO),
        version=7,
    )
    back = Resolved.model_validate_json(r.model_dump_json())
    assert back == r
    assert set(back.values) == set(FEATURES)
