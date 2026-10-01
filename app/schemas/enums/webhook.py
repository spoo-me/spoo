"""Webhook system enums."""

from __future__ import annotations

from enum import Enum


class WebhookStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"  # user-initiated
    DISABLED = "disabled"  # system-disabled (410 or consecutive failures) or admin


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class WebhookFlavor(str, Enum):
    """Presentation of the payload — never a second schema. ``raw`` is the
    versioned contract; other flavors are lossy renderings of it."""

    RAW = "raw"
    DISCORD = "discord"
    SLACK = "slack"


class EndpointDisabledReason(str, Enum):
    GONE = "gone"  # endpoint returned 410
    CONSECUTIVE_FAILURES = "consecutive_failures"
    # Stored secret no longer decrypts (master key rotated) — every future
    # delivery would fail identically, so the endpoint is disabled loudly.
    SECRET_UNREADABLE = "secret_unreadable"
    ADMIN = "admin"
