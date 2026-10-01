"""
Feature flag rollout strategies.

Each rollout type defines how `FeatureFlagService.is_enabled` decides whether
a given user sees a flag. Strategies are mutually exclusive; a flag picks
exactly one ``RolloutType`` and configures the corresponding fields on
``FeatureFlagDoc``.

OFF acts as a kill switch independent of the doc-level ``enabled`` boolean —
useful when you want to keep the flag registered but force a False result
without rewriting the rest of the doc.
"""

from __future__ import annotations

from enum import Enum


class RolloutType(str, Enum):
    """How a feature flag decides which users are inside the rollout."""

    OFF = "off"
    EVERYONE = "everyone"
    ALLOWLIST = "allowlist"
    PERCENTAGE = "percentage"
    HEX_DIGIT = "hex_digit"
    TIER = "tier"
