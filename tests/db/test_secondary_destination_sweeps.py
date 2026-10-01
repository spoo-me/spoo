"""The sweep aggregations run against real documents: $zip, $objectToArray
and the pair gate are only ever exercised here, not by the unit mocks.
Hosts use .com: a made-up TLD is its own registrable domain to tldextract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId

from app.repositories.url_repository import UrlRepository
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.schemas.models.url import UrlDestination


async def _insert(
    real_db, alias: str, long_url: str, *, geo_rules=None, pre_start_url=None, dest=None
):
    stamp = (
        dest
        if dest is not None
        else UrlDestination.for_link(
            long_url, geo_rules=geo_rules, pre_start_url=pre_start_url
        ).to_doc()
    )
    await real_db["urlsV2"].insert_one(
        {
            "_id": ObjectId(),
            "alias": alias,
            "domain": "spoo.me",
            "owner_id": ANONYMOUS_OWNER_ID,
            "created_at": datetime.now(timezone.utc),
            "long_url": long_url,
            "geo_rules": geo_rules,
            "pre_start_url": pre_start_url,
            "dest": stamp,
            "status": "ACTIVE",
            "total_clicks": 0,
        }
    )


async def test_feed_sweep_reaches_hidden_hosts_and_samples_their_own_url(real_db):
    repo = UrlRepository(real_db["urlsV2"])
    await _insert(
        real_db,
        "geo1",
        "https://clean-test.com/landing",
        geo_rules={"IN": "https://kit.evil-feed-test.com/in"},
    )
    await _insert(real_db, "main1", "https://shop.evil-feed-test.com/x")
    await _insert(
        real_db,
        "pre1",
        "https://clean-test.com/other",
        pre_start_url="https://teaser.evil-feed-test.com/soon",
    )

    hosts = dict(await repo.list_active_hosts_by_registrable("evil-feed-test.com"))

    # Every host under the feed domain, each with a URL on that host.
    assert hosts == {
        "kit.evil-feed-test.com": "https://kit.evil-feed-test.com/in",
        "shop.evil-feed-test.com": "https://shop.evil-feed-test.com/x",
        "teaser.evil-feed-test.com": "https://teaser.evil-feed-test.com/soon",
    }
    # The clean main host is never enqueued for a domain it does not belong to.
    assert "clean-test.com" not in hosts
    assert dict(await repo.list_active_hosts_by_registrable("clean-test.com")) == {
        "clean-test.com": "https://clean-test.com/landing"
    }


async def test_recent_screen_lists_secondary_hosts_with_their_own_url(real_db):
    repo = UrlRepository(real_db["urlsV2"])
    await _insert(
        real_db,
        "geo2",
        "https://clean-test.com/landing",
        geo_rules={"BR": "https://hidden-test.com/kit"},
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    hosts = dict(await repo.list_recent_destination_hosts(since))
    assert hosts["clean-test.com"] == "https://clean-test.com/landing"
    assert hosts["hidden-test.com"] == "https://hidden-test.com/kit"


async def test_pre_registrable_stamp_is_invisible_to_the_feed_sweep_until_backfilled(
    real_db,
):
    """A doc stamped by the first release (secondary_hosts only) yields no
    (host, registrable) pairs: $zip stops at the shorter list. The
    backfill's second pass keys on the missing field and repairs it."""
    repo = UrlRepository(real_db["urlsV2"])
    await _insert(
        real_db,
        "old1",
        "https://clean-test.com/old",
        geo_rules={"IN": "https://kit.evil-feed-test.com/old"},
        dest={
            "scheme": "https",
            "host": "clean-test.com",
            "subdomain": "",
            "registrable_domain": "clean-test.com",
            "secondary_hosts": ["kit.evil-feed-test.com"],
        },
    )
    assert await repo.list_active_hosts_by_registrable("evil-feed-test.com") == []
    # Host verdicts still reach it: the host filter does not need the registrable.
    assert [
        a
        for a, _, _ in await repo.list_by_dest_host_with_urls("kit.evil-feed-test.com")
    ] == ["old1"]
