"""Tripwire: A/B variants must not go live before safety can see them.

Variants add destinations exactly like geo rules do. Safety enforcement
matches ``dest.host`` or ``dest.secondary_hosts``; a variant host that is
never stamped there matches nothing. AB_VARIANTS_ENABLED keeps the feature
dark; this test fails the moment it is flipped without the stamping work.
"""

from __future__ import annotations

import inspect
import typing

from app.config import AppSettings
from app.schemas.models.url import UrlDestination
from app.shared.url_utils import link_destination_urls_for

_FLAGS = ("ab_testing_enabled", "ab_variants_enabled")

_WORK = """
A/B variants were enabled but safety cannot see variant destinations.
Required before shipping them:

  1. pass the variant URLs as ``variants=`` to UrlDestination.for_link at
     every write point (create, update, backfill) so secondary_hosts
     carries them
  2. include variant URLs in link_destination_urls callers (report intake,
     hot screening, the L1 create-time burst counter) via
     shared.url_utils.link_destination_urls_for, the one place that reads
     a link's fields, so all three stay in sync as fields are added
  3. stamp existing links: scripts/backfill_url_dest.py
  4. add the variant URLs to _ALL_URLS in repositories/url_repository.py so
     the sweeps sample a variant host's own URL (today: long_url, geo rules,
     ab_variants, and every field in shared.url_utils.SINGLE_DESTINATION_FIELDS)
"""


def test_ab_variants_stay_dark_until_enforcement_sees_them():
    settings = AppSettings()
    if not any(getattr(settings, flag, False) for flag in _FLAGS):
        return
    assert "secondary_hosts" in UrlDestination.model_fields, _WORK
    assert "variants" in inspect.signature(UrlDestination.for_link).parameters, _WORK
    stamped = UrlDestination.for_link(
        "https://main.example/", variants=["https://variant.example/b"]
    )
    assert stamped is not None and stamped.secondary_hosts == ["variant.example"], _WORK
    assert stamped.secondary_registrable == ["variant.example"], _WORK

    class _FakeLink:
        long_url = "https://main.example/"
        geo_rules = None
        ab_variants: typing.ClassVar = [
            {"url": "https://variant.example/b", "weight": 40}
        ]

    assert "https://variant.example/b" in link_destination_urls_for(_FakeLink()), _WORK
