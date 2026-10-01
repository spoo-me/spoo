"""Shared test object factories.

One place for the UrlCacheData / ClickEvent shapes used across unit and
integration tests — previously duplicated per test module.
"""

from __future__ import annotations

from app.infrastructure.cache.url_cache import UrlCacheData
from app.services.click.events import ClickEvent


def make_url_cache(**overrides) -> UrlCacheData:
    base = dict(
        _id="507f1f77bcf86cd799439011",
        alias="abc1234",
        long_url="https://example.com",
        block_bots=False,
        password_hash=None,
        expiration_time=None,
        max_clicks=None,
        url_status="ACTIVE",
        schema_version="v2",
        owner_id=None,
        total_clicks=0,
        domain="spoo.me",
    )
    base.update(overrides)
    return UrlCacheData(**base)


def make_click_event(**overrides) -> ClickEvent:
    base = dict(
        short_code="abc",
        schema_key="v2",
        is_emoji=False,
        url=make_url_cache(alias="abc"),
        client_ip="1.2.3.4",
        user_agent="Mozilla/5.0",
        referrer="https://t.co/x",
        cf_city="Berlin",
        redirect_ms=7,
    )
    base.update(overrides)
    return ClickEvent(**base)
