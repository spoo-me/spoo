"""Origin side of the edge-cache contract.

``edge/spoo-edge-cache/contract/fixtures.json`` is the single source of
truth for the KV key format and entry JSON. This suite proves the Python
side EMITS exactly those shapes; the Worker's vitest suite proves the JS
side SERVES them. A change that breaks either suite is a contract change
and must update fixtures + both sides together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.edge_cache import (
    EdgeCacheEntry,
    EdgeCacheGeoEntry,
    cache_key,
)

_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "edge"
    / "spoo-edge-cache"
    / "contract"
    / "fixtures.json"
)


def _fixtures() -> dict:
    return json.loads(_FIXTURES.read_text())


def test_fixture_file_exists_where_the_worker_expects_it():
    assert _FIXTURES.is_file(), (
        "contract fixtures moved — update this path AND the worker tests"
    )


@pytest.mark.parametrize(
    "fixture",
    _fixtures()["entries"],
    ids=lambda f: f["name"],
)
def test_python_emits_exactly_the_fixture_shapes(fixture):
    domain, _, code = fixture["key"].removeprefix("cache:").partition(":")
    assert cache_key(domain, code) == fixture["key"]

    entry = EdgeCacheEntry(
        url=fixture["value"]["url"], status=fixture["value"]["status"]
    )
    # Wire format omits absent optionals (og_html) — a plain redirect must
    # stay byte-compatible with pre-meta-tags workers.
    assert json.loads(entry.to_kv_json()) == fixture["value"]


@pytest.mark.parametrize(
    "fixture",
    _fixtures()["geo_entries"],
    ids=lambda f: f["name"],
)
def test_python_emits_exactly_the_geo_fixture_shapes(fixture):
    domain, _, code = fixture["key"].removeprefix("cache:").partition(":")
    assert cache_key(domain, code) == fixture["key"]

    entry = EdgeCacheGeoEntry(
        url=fixture["value"]["url"],
        status=fixture["value"]["status"],
        rules=fixture["value"]["rules"],
    )
    assert json.loads(entry.to_kv_json()) == fixture["value"]


@pytest.mark.parametrize(
    "fixture",
    _fixtures()["og_entries"],
    ids=lambda f: f["name"],
)
def test_python_emits_exactly_the_og_fixture_shapes(fixture):
    domain, _, code = fixture["key"].removeprefix("cache:").partition(":")
    assert cache_key(domain, code) == fixture["key"]

    value = fixture["value"]
    if value["type"] == "geo_redirect":
        entry: EdgeCacheEntry | EdgeCacheGeoEntry = EdgeCacheGeoEntry(
            url=value["url"],
            status=value["status"],
            rules=value["rules"],
            og_html=value["og_html"],
        )
    else:
        entry = EdgeCacheEntry(
            type=value["type"],
            url=value.get("url"),
            status=value["status"],
            og_html=value["og_html"],
        )
    assert json.loads(entry.to_kv_json()) == value
