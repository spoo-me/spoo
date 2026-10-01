"""Catalog drift test: every feature is declared completely, once.

This is the test that fails when someone adds a feature and forgets half
of it. Each assertion names the field it guards so the failure reads as an
instruction.
"""

from __future__ import annotations

import re

from services.features.catalog import (
    FEATURES,
    UNLIMITED,
    Kind,
    Plan,
    bool_features,
    int_features,
    plan_defaults,
)

_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def test_keys_are_snake_case_and_match_entries():
    for key, feature in FEATURES.items():
        assert _KEY.match(key), f"{key}: not snake_case"
        assert feature.key == key, f"{key}: entry key mismatch"


def test_every_feature_has_all_four_plan_defaults():
    for key, feature in FEATURES.items():
        assert set(feature.plans) == set(Plan), f"{key}: plans must cover every Plan"


def test_plan_default_types_match_kind():
    for key, feature in FEATURES.items():
        for plan, value in feature.plans.items():
            if feature.kind is Kind.BOOL:
                assert isinstance(value, bool), f"{key}[{plan}]: BOOL needs a bool"
            else:
                assert isinstance(value, int) and not isinstance(value, bool), (
                    f"{key}[{plan}]: INT needs an int"
                )


def test_bool_features_declare_lapse_and_no_over_limit():
    for feature in bool_features():
        assert feature.lapse is not None, f"{feature.key}: missing lapse policy"
        assert feature.over_limit is None, f"{feature.key}: over_limit is for INT"


def test_int_features_declare_no_lapse_or_rollout():
    for feature in int_features():
        assert feature.lapse is None, f"{feature.key}: lapse is for BOOL"
        assert feature.rollout is None, f"{feature.key}: limits have no rollout"


def test_selfhost_holds_everything():
    for key, value in plan_defaults(Plan.SELFHOST).items():
        expected = True if FEATURES[key].kind is Kind.BOOL else UNLIMITED
        assert value == expected, f"{key}: selfhost must be {expected!r}"


def test_pro_is_at_least_free():
    for key, feature in FEATURES.items():
        free, pro = feature.plans[Plan.FREE], feature.plans[Plan.PRO]
        if feature.kind is Kind.BOOL:
            assert pro or not free, f"{key}: free has it but pro does not"
        else:
            assert pro == UNLIMITED or pro >= free, f"{key}: pro below free"


def test_anonymous_never_exceeds_free():
    for key, feature in FEATURES.items():
        anon, free = feature.plans[Plan.ANONYMOUS], feature.plans[Plan.FREE]
        if feature.kind is Kind.BOOL:
            assert free or not anon, f"{key}: anonymous has it but free does not"
        else:
            assert anon <= free, f"{key}: anonymous above free"


def test_rollout_names_are_unique():
    rollouts = [f.rollout for f in FEATURES.values() if f.rollout is not None]
    assert len(rollouts) == len(set(rollouts)), "two features share a flag document"
