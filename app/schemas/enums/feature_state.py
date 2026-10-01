"""Per-user feature availability, as exposed to clients."""

from enum import Enum


class FeatureState(str, Enum):
    """What a client should do with a gated feature's UI.

    ENABLED — render it; the account has the feature.
    HIDDEN  — render nothing; the feature does not exist for this account.
    LOCKED  — render it in a locked/upsell state. Reserved: no backend
              policy emits it until paid plans exist, but it is part of the
              contract from day one so entitlements later are a data change,
              not an API version bump.
    """

    ENABLED = "enabled"
    LOCKED = "locked"
    HIDDEN = "hidden"
