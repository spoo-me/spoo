"""Tripwire: geo targeting must not go live before safety can see it.

Safety enforcement keys on ``dest.host``, parsed from ``long_url`` only.
A link's geo destinations are stored in no indexed field, so a toxic
verdict for a geo destination's host matches nothing, reports and hot
screening judge the main destination instead, and burst counters never
see the geo host at all. GEO_RULES_ENABLED keeps the feature dark; this
test fails the moment it is flipped without the enforcement work, and
names the work in its failure message.
"""

from __future__ import annotations

from config import AppSettings
from schemas.models.url import UrlDestination

_WORK = """
geo targeting was enabled (GEO_RULES_ENABLED=true) but safety cannot see
geo destinations. Required before shipping it:

  1. stamp geo destination hosts at every write point, e.g.
     UrlDestination gains `geo_hosts: list[str]`, plus a sparse index
  2. enforcement matches dest.host OR dest.geo_hosts (SafetyEnforcer +
     UrlRepository/legacy repos), so a host block reaches these links
  3. report intake and hot screening emit one event per distinct
     destination, geo included
  4. build_evidence_bundle lists geo destinations so the deep tier
     judges the whole link
  5. scripts/backfill_url_dest.py stamps the new field
"""


def test_geo_targeting_stays_dark_until_enforcement_sees_it():
    if not AppSettings().geo_rules_enabled:
        return
    assert "geo_hosts" in UrlDestination.model_fields, _WORK
