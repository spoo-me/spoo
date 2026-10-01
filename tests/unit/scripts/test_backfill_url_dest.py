"""The backfill stamps secondary hosts on existing geo links, once."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_SPEC = importlib.util.spec_from_file_location(
    "backfill_url_dest", Path(__file__).parents[3] / "scripts" / "backfill_url_dest.py"
)
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)


def test_secondary_hosts_mirror_the_shared_helper():
    from app.shared.url_utils import secondary_hosts

    rules = {
        "IN": "https://B.example/x",
        "US": "https://a.example/",
        "DE": "https://main.example/",
    }
    urls = backfill.secondary_urls(
        {"geo_rules": rules, "pre_start_url": "https://teaser.example/"}
    )
    assert backfill.secondary_hosts(urls, "main.example") == [
        "a.example",
        "b.example",
        "teaser.example",
    ]
    assert backfill.secondary_hosts(urls, "main.example") == secondary_hosts(
        urls, exclude="main.example"
    )
    assert backfill.secondary_urls({"geo_rules": None, "pre_start_url": None}) == []


def test_single_destination_fields_mirror_the_shared_list():
    from app.shared.url_utils import SINGLE_DESTINATION_FIELDS

    assert backfill.SINGLE_DESTINATION_FIELDS == SINGLE_DESTINATION_FIELDS
    assert backfill.secondary_urls(
        {"expired_redirect_url": "https://ended.example/bye"}
    ) == ["https://ended.example/bye"]
    assert {"expired_redirect_url": {"$type": "string"}} in backfill._SECONDARY_FILTER[
        "$and"
    ][0]["$or"]


def test_secondary_fields_are_index_aligned():
    fields = backfill.secondary_fields(
        ["https://shop.evil.co.uk/x", "https://a.evil.com/y"], "main.example"
    )
    assert fields == {
        "secondary_hosts": ["a.evil.com", "shop.evil.co.uk"],
        "secondary_registrable": ["evil.com", "evil.co.uk"],
    }


def test_secondary_hosts_include_variant_destinations():
    variants = [
        {"url": "https://B.example/x", "weight": 40},
        {"url": "https://main.example/b", "weight": 10},
    ]
    urls = backfill.secondary_urls({"ab_variants": variants})
    assert backfill.secondary_hosts(urls, "main.example") == ["b.example"]
    both_urls = backfill.secondary_urls(
        {"geo_rules": {"IN": "https://geo.example/"}, "ab_variants": variants}
    )
    assert backfill.secondary_hosts(both_urls, "main.example") == [
        "b.example",
        "geo.example",
    ]


def test_dest_for_includes_secondary_hosts_only_when_present():
    plain = backfill.dest_for({"long_url": "https://main.example/p"}, "long_url")
    assert plain["host"] == "main.example" and "secondary_hosts" not in plain
    geo = backfill.dest_for(
        {
            "long_url": "https://main.example/p",
            "geo_rules": {"IN": "https://geo.example/"},
        },
        "long_url",
    )
    assert geo["secondary_hosts"] == ["geo.example"]
    assert geo["secondary_registrable"] == ["geo.example"]
    variant = backfill.dest_for(
        {
            "long_url": "https://main.example/p",
            "ab_variants": [{"url": "https://variant.example/b", "weight": 50}],
        },
        "long_url",
    )
    assert variant["secondary_hosts"] == ["variant.example"]
    assert backfill.dest_for({"long_url": "nonsense"}, "long_url") is None
    scheduled = backfill.dest_for(
        {
            "long_url": "https://main.example/",
            "pre_start_url": "https://teaser.example/",
        },
        "long_url",
    )
    assert scheduled["secondary_hosts"] == ["teaser.example"]


def test_backfill_secondary_stamps_and_reports(capsys):
    docs = [
        {
            "_id": 1,
            "dest": {"host": "main.example"},
            "geo_rules": {"IN": "https://geo.example/"},
        },
        {
            "_id": 2,
            "dest": {"host": "main.example"},
            "geo_rules": {"IN": "https://main.example/in"},
        },
    ]
    col = MagicMock()
    col.name = "urlsV2"
    col.count_documents = MagicMock(side_effect=[2, 0])
    cursor = MagicMock()
    cursor.sort.return_value.limit = MagicMock(side_effect=[docs, []])
    col.find = MagicMock(return_value=cursor)
    col.bulk_write = MagicMock(return_value=MagicMock(matched_count=2))

    backfill.backfill_secondary(col, dry_run=False)

    ops = col.bulk_write.call_args.args[0]
    # Optimistic predicate: the write re-asserts the state it was computed from.
    assert ops[0]._filter == {
        "_id": 1,
        "geo_rules": {"IN": "https://geo.example/"},
        "ab_variants": None,
        "pre_start_url": None,
        "expired_redirect_url": None,
        "dest.host": "main.example",
        "dest.secondary_registrable": {"$exists": False},
    }
    sets = {op._filter["_id"]: op._doc["$set"] for op in ops}
    # A rule that adds no new host still gets [] so the filter converges.
    assert sets == {
        1: {
            "dest.secondary_hosts": ["geo.example"],
            "dest.secondary_registrable": ["geo.example"],
        },
        2: {"dest.secondary_hosts": [], "dest.secondary_registrable": []},
    }
    out = capsys.readouterr().out
    assert "geo/variant/scheduled links needing secondary fields: 2" in out
    assert (
        "secondary fields stamped 2; failed: 0; skipped (changed meanwhile): 0; "
        "remaining (expect 0): 0"
    ) in out


def test_backfill_secondary_counts_docs_that_changed_under_it(capsys):
    docs = [
        {
            "_id": 1,
            "dest": {"host": "main.example"},
            "geo_rules": {"IN": "https://geo.example/"},
        },
    ]
    col = MagicMock()
    col.name = "urlsV2"
    col.count_documents = MagicMock(side_effect=[1, 1])
    cursor = MagicMock()
    cursor.sort.return_value.limit = MagicMock(side_effect=[docs, []])
    col.find = MagicMock(return_value=cursor)
    # The doc was edited between read and write: the predicate matched nothing.
    col.bulk_write = MagicMock(return_value=MagicMock(matched_count=0))

    backfill.backfill_secondary(col, dry_run=False)

    out = capsys.readouterr().out
    assert (
        "stamped 0; failed: 0; skipped (changed meanwhile): 1; remaining (expect 1): 1"
        in out
    )


def test_backfill_secondary_dry_run_writes_nothing(capsys):
    col = MagicMock()
    col.name = "urlsV2"
    col.count_documents = MagicMock(return_value=5)
    backfill.backfill_secondary(col, dry_run=True)
    col.bulk_write.assert_not_called()
    assert "needing secondary fields: 5" in capsys.readouterr().out


def test_backfill_secondary_partial_bulk_failure_reconciles(capsys):
    from pymongo.errors import BulkWriteError

    docs = [
        {
            "_id": i,
            "dest": {"host": "main.example"},
            "geo_rules": {"IN": "https://geo.example/"},
        }
        for i in (1, 2, 3)
    ]
    col = MagicMock()
    col.name = "urlsV2"
    col.count_documents = MagicMock(side_effect=[3, 2])
    cursor = MagicMock()
    cursor.sort.return_value.limit = MagicMock(side_effect=[docs, []])
    col.find = MagicMock(return_value=cursor)
    # One op failed, one matched and wrote, one matched nothing (changed meanwhile).
    col.bulk_write = MagicMock(
        side_effect=BulkWriteError({"nMatched": 1, "writeErrors": [{"index": 0}]})
    )

    backfill.backfill_secondary(col, dry_run=False)

    out = capsys.readouterr().out
    assert (
        "stamped 1; failed: 1; skipped (changed meanwhile): 1; remaining (expect 2): 2"
        in out
    )


def test_secondary_pass_repairs_a_null_dest_with_valid_geo_hosts():
    stamp = backfill._secondary_set(
        {
            "_id": 9,
            "dest": None,
            "long_url": "nonsense",
            "geo_rules": {"IN": "https://geo.example/"},
        }
    )
    assert stamp == {
        "dest": {
            "scheme": "",
            "host": "",
            "subdomain": "",
            "registrable_domain": "",
            "secondary_hosts": ["geo.example"],
            "secondary_registrable": ["geo.example"],
        }
    }
    # Nothing parseable at all still converges instead of matching forever.
    nothing = backfill._secondary_set(
        {
            "_id": 10,
            "dest": None,
            "long_url": "nonsense",
            "geo_rules": {"IN": "also nonsense"},
        }
    )
    assert nothing["dest"]["secondary_hosts"] == []
    assert nothing["dest"]["secondary_registrable"] == []
    assert backfill._SECONDARY_FILTER["$and"][1]["$or"][1] == {"dest": None}
