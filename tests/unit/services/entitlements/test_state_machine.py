"""Subscription state machine: every state times every event, explicitly."""

from __future__ import annotations

import pytest

from errors import InvalidTransitionError
from schemas.models.subscription import SubscriptionStatus as S
from services.entitlements.state_machine import (
    SubscriptionEvent as E,
)
from services.entitlements.state_machine import (
    is_noop,
    next_status,
)

REJECT = "reject"

# Rows: current status (None = no subscription); columns: EVENTS in order.
# A status means the transition lands there; REJECT means it cannot arrive.
EVENTS = (
    E.GRANTED,
    E.PAYMENT_SUCCEEDED,
    E.PAYMENT_FAILED,
    E.CANCEL_SCHEDULED,
    E.CANCEL_REVOKED,
    E.TERM_ENDED,
    E.GRACE_ENDED,
    E.PAST_DUE_CEILING,
    E.ENDED,
)
TABLE = {
    None: (S.ACTIVE, REJECT, REJECT, REJECT, REJECT, REJECT, REJECT, REJECT, REJECT),
    S.ACTIVE: (
        S.ACTIVE,
        S.ACTIVE,
        S.PAST_DUE,
        S.CANCEL_AT_PERIOD_END,
        S.ACTIVE,
        S.GRACE,
        REJECT,
        REJECT,
        S.LAPSED,
    ),
    S.PAST_DUE: (
        S.ACTIVE,
        S.ACTIVE,
        S.PAST_DUE,
        S.CANCEL_AT_PERIOD_END,
        REJECT,
        REJECT,
        REJECT,
        S.LAPSED,
        S.LAPSED,
    ),
    S.CANCEL_AT_PERIOD_END: (
        S.ACTIVE,
        S.CANCEL_AT_PERIOD_END,
        S.PAST_DUE,
        S.CANCEL_AT_PERIOD_END,
        S.ACTIVE,
        S.GRACE,
        REJECT,
        REJECT,
        S.LAPSED,
    ),
    S.GRACE: (
        S.ACTIVE,
        REJECT,
        REJECT,
        REJECT,
        REJECT,
        S.GRACE,
        S.LAPSED,
        REJECT,
        S.LAPSED,
    ),
    S.LAPSED: (
        S.ACTIVE,
        REJECT,
        REJECT,
        REJECT,
        REJECT,
        S.LAPSED,
        S.LAPSED,
        S.LAPSED,
        S.LAPSED,
    ),
}


def _cases():
    for current, row in TABLE.items():
        assert len(row) == len(EVENTS)
        for event, expected in zip(EVENTS, row, strict=True):
            yield current, event, expected


@pytest.mark.parametrize(
    ("current", "event", "expected"),
    list(_cases()),
    ids=lambda v: getattr(v, "value", str(v)),
)
def test_every_state_times_every_event(current, event, expected):
    if expected == REJECT:
        with pytest.raises(InvalidTransitionError):
            next_status(current, event)
        assert is_noop(current, event) is False
    else:
        assert next_status(current, event) is expected
        assert is_noop(current, event) is (expected == current)


def test_table_covers_every_state_and_event():
    assert set(TABLE) == {None, *S}
    assert set(EVENTS) == set(E)


def test_payment_never_reactivates_a_finished_subscription():
    for current in (S.GRACE, S.LAPSED):
        with pytest.raises(InvalidTransitionError):
            next_status(current, E.PAYMENT_SUCCEEDED)
