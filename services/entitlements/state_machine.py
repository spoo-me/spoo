"""
Subscription status state machine.

``next_status(current, event)`` is the only place a status may change. It
returns the status after the event, the same status when the event is a
no-op in that state, and raises ``InvalidTransitionError`` when the event
cannot legally arrive in that state. Backward moves from payment events
(a late ``payment_succeeded`` on a grace or lapsed subscription) are
rejections, not transitions: only ``GRANTED`` reactivates.
"""

from __future__ import annotations

from enum import Enum

from errors import InvalidTransitionError
from schemas.models.subscription import SubscriptionStatus as S


class SubscriptionEvent(str, Enum):
    # A new checkout, a repurchase, or a manual grant.
    GRANTED = "granted"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"
    CANCEL_SCHEDULED = "cancel_scheduled"
    CANCEL_REVOKED = "cancel_revoked"
    TERM_ENDED = "term_ended"
    GRACE_ENDED = "grace_ended"
    PAST_DUE_CEILING = "past_due_ceiling"
    # Immediate end with no grace: a full refund or a manual end.
    ENDED = "ended"


E = SubscriptionEvent

# (current status, event) -> next status. Absent pairs are rejected.
# ``None`` as the current status means no subscription exists yet.
_TRANSITIONS: dict[tuple[S | None, E], S] = {
    (None, E.GRANTED): S.ACTIVE,
    (S.ACTIVE, E.GRANTED): S.ACTIVE,
    (S.PAST_DUE, E.GRANTED): S.ACTIVE,
    (S.CANCEL_AT_PERIOD_END, E.GRANTED): S.ACTIVE,
    (S.GRACE, E.GRANTED): S.ACTIVE,
    (S.LAPSED, E.GRANTED): S.ACTIVE,
    (S.ACTIVE, E.PAYMENT_SUCCEEDED): S.ACTIVE,
    (S.PAST_DUE, E.PAYMENT_SUCCEEDED): S.ACTIVE,
    (S.CANCEL_AT_PERIOD_END, E.PAYMENT_SUCCEEDED): S.CANCEL_AT_PERIOD_END,
    (S.ACTIVE, E.PAYMENT_FAILED): S.PAST_DUE,
    (S.PAST_DUE, E.PAYMENT_FAILED): S.PAST_DUE,
    (S.CANCEL_AT_PERIOD_END, E.PAYMENT_FAILED): S.PAST_DUE,
    (S.ACTIVE, E.CANCEL_SCHEDULED): S.CANCEL_AT_PERIOD_END,
    (S.PAST_DUE, E.CANCEL_SCHEDULED): S.CANCEL_AT_PERIOD_END,
    (S.CANCEL_AT_PERIOD_END, E.CANCEL_SCHEDULED): S.CANCEL_AT_PERIOD_END,
    (S.ACTIVE, E.CANCEL_REVOKED): S.ACTIVE,
    (S.CANCEL_AT_PERIOD_END, E.CANCEL_REVOKED): S.ACTIVE,
    (S.ACTIVE, E.TERM_ENDED): S.GRACE,
    (S.CANCEL_AT_PERIOD_END, E.TERM_ENDED): S.GRACE,
    (S.GRACE, E.TERM_ENDED): S.GRACE,
    (S.LAPSED, E.TERM_ENDED): S.LAPSED,
    (S.GRACE, E.GRACE_ENDED): S.LAPSED,
    (S.LAPSED, E.GRACE_ENDED): S.LAPSED,
    (S.PAST_DUE, E.PAST_DUE_CEILING): S.LAPSED,
    (S.LAPSED, E.PAST_DUE_CEILING): S.LAPSED,
    (S.ACTIVE, E.ENDED): S.LAPSED,
    (S.PAST_DUE, E.ENDED): S.LAPSED,
    (S.CANCEL_AT_PERIOD_END, E.ENDED): S.LAPSED,
    (S.GRACE, E.ENDED): S.LAPSED,
    (S.LAPSED, E.ENDED): S.LAPSED,
}


def next_status(current: S | None, event: E) -> S:
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError:
        raise InvalidTransitionError(
            f"event {event.value!r} cannot apply to status "
            f"{current.value if current else 'none'!r}"
        ) from None
