"""Unit tests for the L3 sweeps."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config import SafetySettings
from services.safety.sweeps import (
    RECENT_SCREEN_TASK,
    FeedDeltaSweeper,
    SweepDeps,
    build_sweep_tasks,
    recent_screen_task,
)


class TestFeedDeltaSweeper:
    @pytest.mark.asyncio
    async def test_enqueues_one_event_per_active_host(self):
        url_repo = AsyncMock()
        url_repo.list_active_hosts_by_registrable = AsyncMock(
            return_value=[
                ("login.evil.com", "https://login.evil.com/a"),
                ("pay.evil.com", "https://pay.evil.com/b"),
            ]
        )
        sink = AsyncMock()

        enqueued = await FeedDeltaSweeper(url_repo, sink).sweep(
            "fishfish", {"evil.com"}
        )

        assert enqueued == 2
        events = [c.args[0] for c in sink.emit.await_args_list]
        assert {e.host for e in events} == {"login.evil.com", "pay.evil.com"}
        assert all(e.trigger == "sweep" for e in events)
        assert events[0].context == {"sweep": "feed_delta", "feed": "fishfish"}

    @pytest.mark.asyncio
    async def test_no_active_links_enqueues_nothing(self):
        url_repo = AsyncMock()
        url_repo.list_active_hosts_by_registrable = AsyncMock(return_value=[])
        sink = AsyncMock()

        assert (
            await FeedDeltaSweeper(url_repo, sink).sweep(
                "fishfish", {"nobody-links-here.com"}
            )
            == 0
        )
        sink.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_bad_domain_never_aborts_the_delta(self):
        url_repo = AsyncMock()
        url_repo.list_active_hosts_by_registrable = AsyncMock(
            side_effect=[RuntimeError("mongo hiccup"), [("ok.evil.com", "u")]]
        )
        sink = AsyncMock()

        enqueued = await FeedDeltaSweeper(url_repo, sink).sweep(
            "fishfish", {"a.com", "b.com"}
        )

        assert enqueued == 1


class TestRecentScreenTask:
    def _deps(self, hosts, judged):
        url_repo = AsyncMock()
        url_repo.list_recent_destination_hosts = AsyncMock(return_value=hosts)
        verdict_repo = AsyncMock()
        verdict_repo.hosts_with_verdicts = AsyncMock(return_value=judged)
        sink = AsyncMock()
        return SweepDeps(url_repo=url_repo, verdict_repo=verdict_repo, sink=sink), sink

    def test_task_shape(self):
        deps, _ = self._deps([], set())
        task = recent_screen_task(deps, window_hours=48, max_enqueues=1000)
        assert task.name == RECENT_SCREEN_TASK
        assert task.schedule == "30 * * * *"

    @pytest.mark.asyncio
    async def test_only_unjudged_hosts_are_enqueued(self):
        deps, sink = self._deps(
            [
                ("known.com", "https://known.com/a"),
                ("fresh.com", "https://fresh.com/b"),
            ],
            {"known.com"},
        )

        detail = await recent_screen_task(deps, window_hours=48, max_enqueues=1000).fn()

        assert detail == {"hosts_seen": 2, "novel": 1, "enqueued": 1}
        event = sink.emit.await_args.args[0]
        assert event.host == "fresh.com"
        assert event.trigger == "sweep"
        assert event.context == {"sweep": "recent_screen"}

    @pytest.mark.asyncio
    async def test_registrable_domain_is_derived_for_the_event(self):
        deps, sink = self._deps(
            [("a.b.fresh.co.uk", "https://a.b.fresh.co.uk/x")], set()
        )
        await recent_screen_task(deps, window_hours=48, max_enqueues=10).fn()
        assert sink.emit.await_args.args[0].registrable_domain == "fresh.co.uk"

    @pytest.mark.asyncio
    async def test_cap_bounds_the_run(self):
        hosts = [(f"h{i}.com", f"https://h{i}.com") for i in range(10)]
        deps, sink = self._deps(hosts, set())

        detail = await recent_screen_task(deps, window_hours=48, max_enqueues=4).fn()

        assert detail == {"hosts_seen": 10, "novel": 10, "enqueued": 4}
        assert sink.emit.await_count == 4

    @pytest.mark.asyncio
    async def test_nothing_recent_is_a_clean_noop(self):
        deps, sink = self._deps([], set())
        detail = await recent_screen_task(deps, window_hours=48, max_enqueues=10).fn()
        assert detail == {"hosts_seen": 0, "novel": 0, "enqueued": 0}
        sink.emit.assert_not_awaited()


class TestSweepCatalog:
    def _deps(self):
        return SweepDeps(
            url_repo=AsyncMock(), verdict_repo=AsyncMock(), sink=AsyncMock()
        )

    def test_registered_when_safety_and_sweep_enabled(self):
        tasks = build_sweep_tasks(SafetySettings(enabled=True), self._deps())
        assert [t.name for t in tasks] == [RECENT_SCREEN_TASK]

    def test_absent_when_safety_off(self):
        assert build_sweep_tasks(SafetySettings(enabled=False), self._deps()) == []

    def test_absent_when_sweep_disabled(self):
        settings = SafetySettings(enabled=True, sweep_recent_enabled=False)
        assert build_sweep_tasks(settings, self._deps()) == []
