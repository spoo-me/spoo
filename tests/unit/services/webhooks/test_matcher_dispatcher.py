"""Matcher + dispatcher — scope evaluation, cache short-circuit, fan-out,
pending cap. All fakes are in-memory; no Mongo, no Redis."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from app.schemas.enums.webhook import WebhookStatus
from app.schemas.models.webhook import WebhookEndpointDoc, WebhookScope
from app.services.events.contract import DomainEvent
from app.services.webhooks.dispatcher import WebhookDispatcher
from app.services.webhooks.matcher import (
    OwnerSubscriptionCache,
    SubscriptionMatcher,
    event_link_id,
)

_OWNER = ObjectId()
_LINK = ObjectId()


def _endpoint(**overrides: Any) -> WebhookEndpointDoc:
    doc = WebhookEndpointDoc(
        user_id=_OWNER,
        url="https://example.com/hook",
        events=["link.*"],
        status=WebhookStatus.ACTIVE,
        signing_secret_enc="enc",
        signing_secret_prefix="whsec_ab",
    )
    doc.id = ObjectId()
    return doc.model_copy(update=overrides)


def _click_event(link_id: ObjectId = _LINK) -> DomainEvent:
    return DomainEvent(
        type="link.clicked",
        owner_id=str(_OWNER),
        data={"link_id": str(link_id), "alias": "a"},
    )


def _no_cache() -> OwnerSubscriptionCache:
    return OwnerSubscriptionCache(None, ttl_seconds=60)


class TestEventLinkId:
    def test_top_level_link_id(self):
        assert event_link_id(_click_event()) == _LINK

    def test_nested_snapshot_link_id(self):
        event = DomainEvent(
            type="link.updated",
            owner_id=str(_OWNER),
            data={"link": {"link_id": str(_LINK)}, "changes": {}},
        )
        assert event_link_id(event) == _LINK

    def test_event_without_link_shape_is_none(self):
        event = DomainEvent(
            type="webhook.test", owner_id=str(_OWNER), data={"message": "hi"}
        )
        assert event_link_id(event) is None


class TestMatcher:
    @pytest.mark.asyncio
    async def test_matches_wildcard_subscription(self):
        repo = AsyncMock()
        repo.find_active_for_owner.return_value = [_endpoint()]
        matcher = SubscriptionMatcher(repo, _no_cache())
        assert len(await matcher.match(_click_event())) == 1

    @pytest.mark.asyncio
    async def test_event_type_mismatch_filters_out(self):
        repo = AsyncMock()
        repo.find_active_for_owner.return_value = [_endpoint(events=["domain.*"])]
        matcher = SubscriptionMatcher(repo, _no_cache())
        assert await matcher.match(_click_event()) == []

    @pytest.mark.asyncio
    async def test_scope_excludes_other_links(self):
        repo = AsyncMock()
        repo.find_active_for_owner.return_value = [
            _endpoint(scope=WebhookScope(links=[ObjectId()]))
        ]
        matcher = SubscriptionMatcher(repo, _no_cache())
        assert await matcher.match(_click_event()) == []

    @pytest.mark.asyncio
    async def test_scope_includes_listed_link(self):
        repo = AsyncMock()
        repo.find_active_for_owner.return_value = [
            _endpoint(scope=WebhookScope(links=[_LINK]))
        ]
        matcher = SubscriptionMatcher(repo, _no_cache())
        assert len(await matcher.match(_click_event())) == 1

    @pytest.mark.asyncio
    async def test_unknown_event_type_never_matches(self):
        """Types outside the registry match nothing, even against `*` —
        wildcard expansion is registry-bounded by construction."""
        repo = AsyncMock()
        repo.find_active_for_owner.return_value = [_endpoint(events=["*"])]
        matcher = SubscriptionMatcher(repo, _no_cache())
        event = DomainEvent(type="totally.unknown", owner_id=str(_OWNER), data={})
        assert await matcher.match(event) == []

    @pytest.mark.asyncio
    async def test_cached_zero_short_circuits_mongo(self):
        repo = AsyncMock()
        cache = AsyncMock()
        cache.get.return_value = 0
        matcher = SubscriptionMatcher(repo, cache)
        assert await matcher.match(_click_event()) == []
        repo.find_active_for_owner.assert_not_awaited()


class _CountingEndpointRepo:
    """In-memory stand-in for the atomic reserve/release counter."""

    def __init__(self, pending: int = 0) -> None:
        self.pending = pending
        self.dropped = 0
        self.reserve_calls = 0

    async def reserve_pending(self, endpoint_id, cap):
        self.reserve_calls += 1
        if self.pending >= cap:
            return False
        self.pending += 1
        return True

    async def set_pending_count(self, endpoint_id, count):
        previous, self.pending = self.pending, count
        return previous

    async def increment_dropped(self, endpoint_id):
        self.dropped += 1


class TestDispatcher:
    def _make(self, endpoints, pending: int = 0, real_pending: int | None = None):
        matcher = AsyncMock()
        matcher.match.return_value = endpoints
        event_repo = AsyncMock()
        event_repo.insert_event.return_value = ObjectId()
        delivery_repo = AsyncMock()

        def real_count(_endpoint_id):
            if real_pending is not None:
                return real_pending
            inserted = delivery_repo.insert_many_rows.await_args_list
            return pending + sum(len(call.args[0]) for call in inserted)

        delivery_repo.count_pending.side_effect = real_count
        endpoint_repo = _CountingEndpointRepo(pending)
        dispatcher = WebhookDispatcher(
            matcher,
            event_repo,
            delivery_repo,
            endpoint_repo,
            max_pending_per_endpoint=10,
        )
        return dispatcher, event_repo, delivery_repo, endpoint_repo

    @pytest.mark.asyncio
    async def test_no_match_writes_nothing(self):
        dispatcher, event_repo, delivery_repo, _ = self._make([])
        await dispatcher.dispatch(_click_event())
        event_repo.insert_event.assert_not_awaited()
        delivery_repo.insert_many_rows.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_event_stored_once_rows_per_endpoint(self):
        eps = [_endpoint(), _endpoint()]
        dispatcher, event_repo, delivery_repo, _ = self._make(eps)
        await dispatcher.dispatch(_click_event())
        event_repo.insert_event.assert_awaited_once()
        rows = delivery_repo.insert_many_rows.await_args[0][0]
        assert len(rows) == 2
        # webhook_ids minted per (event x endpoint) and unique
        assert rows[0]["webhook_id"] != rows[1]["webhook_id"]
        # thin rows: no payload, no rendered body at dispatch
        assert "payload" not in rows[0]
        assert rows[0]["rendered_body"] is None

    @pytest.mark.asyncio
    async def test_rows_carry_endpoint_dropped_count(self):
        """The drop counter accumulated over the pending cap rides the next
        dispatched row so the subscriber learns what it missed."""
        ep = _endpoint(dropped_count=7)
        dispatcher, _, delivery_repo, _ = self._make([ep])
        await dispatcher.dispatch(_click_event())
        rows = delivery_repo.insert_many_rows.await_args[0][0]
        assert rows[0]["dropped_since_last"] == 7

    @pytest.mark.asyncio
    async def test_under_cap_reserves_without_counting(self):
        dispatcher, _, delivery_repo, endpoint_repo = self._make([_endpoint()])
        await dispatcher.dispatch(_click_event())
        assert endpoint_repo.pending == 1
        delivery_repo.count_pending.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_cap_drops_and_counts(self):
        """At the cap the verdict is re-verified against one real count,
        then the delivery is dropped and counted."""
        ep = _endpoint()
        dispatcher, _, delivery_repo, endpoint_repo = self._make([ep], pending=10)
        await dispatcher.dispatch(_click_event())
        delivery_repo.count_pending.assert_awaited_once_with(ep.id)
        assert endpoint_repo.dropped == 1
        assert endpoint_repo.pending == 10
        rows = delivery_repo.insert_many_rows.await_args[0][0]
        assert rows == []

    @pytest.mark.asyncio
    async def test_drifted_counter_is_repaired_not_dropped(self):
        """Counter says 10, Mongo says 3: repair to 3, reserve, deliver."""
        dispatcher, _, delivery_repo, endpoint_repo = self._make(
            [_endpoint()], pending=10, real_pending=3
        )
        await dispatcher.dispatch(_click_event())
        assert endpoint_repo.dropped == 0
        assert endpoint_repo.pending == 4
        assert len(delivery_repo.insert_many_rows.await_args[0][0]) == 1

    @pytest.mark.asyncio
    async def test_recount_is_throttled_per_endpoint(self):
        """A confirmed over-cap endpoint pays one real count per interval,
        not one per event."""
        dispatcher, _, delivery_repo, endpoint_repo = self._make([_endpoint()], 10)
        for _ in range(5):
            await dispatcher.dispatch(_click_event())
        delivery_repo.count_pending.assert_awaited_once()
        assert endpoint_repo.dropped == 5

    @pytest.mark.asyncio
    async def test_concurrent_enqueue_never_exceeds_cap(self):
        dispatcher, _, delivery_repo, endpoint_repo = self._make([_endpoint()], 7)
        await asyncio.gather(*(dispatcher.dispatch(_click_event()) for _ in range(20)))
        assert endpoint_repo.pending == 10
        inserted = sum(
            len(call.args[0]) for call in delivery_repo.insert_many_rows.await_args_list
        )
        assert inserted == 3
        assert endpoint_repo.dropped == 17
