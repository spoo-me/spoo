"""Tests for worker telemetry loops."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from structlog.testing import capture_logs

from app.schemas.models.webhook import WebhookEndpointDoc
from app.workers.telemetry import (
    StaleConsumerJanitor,
    StreamMetricsReporter,
    WebhookDepthReporter,
)


def _redis_with_groups(groups, consumers_by_group=None) -> AsyncMock:
    redis = AsyncMock()
    redis.xlen.return_value = 42
    redis.xinfo_groups.return_value = groups
    if consumers_by_group is not None:
        redis.xinfo_consumers.side_effect = lambda _s, g: consumers_by_group[g]
    return redis


class TestStreamMetricsReporter:
    async def test_report_once_reads_backlog_and_groups(self):
        redis = _redis_with_groups([{"name": "stats", "pending": 3, "lag": 17}])
        reporter = StreamMetricsReporter(redis, "events:clicks", 30.0)

        await reporter.report_once()  # must not raise

        redis.xlen.assert_awaited_once_with("events:clicks")
        redis.xinfo_groups.assert_awaited_once_with("events:clicks")


class TestStaleConsumerJanitor:
    def _janitor(self, redis) -> StaleConsumerJanitor:
        return StaleConsumerJanitor(redis, "events:clicks", idle_threshold_ms=1000)

    async def test_removes_dead_idle_consumers(self):
        redis = _redis_with_groups(
            [{"name": "stats"}],
            {
                "stats": [
                    {"name": "stats-old-1", "pending": 0, "idle": 999_999},
                    {"name": "stats-live-2", "pending": 0, "idle": 50},
                ]
            },
        )
        removed = await self._janitor(redis).sweep_once()

        assert removed == 1
        redis.xgroup_delconsumer.assert_awaited_once_with(
            "events:clicks", "stats", "stats-old-1"
        )

    async def test_never_removes_consumers_with_pending(self):
        """Deleting a consumer with pending would orphan its PEL entries."""
        redis = _redis_with_groups(
            [{"name": "stats"}],
            {"stats": [{"name": "stats-dead", "pending": 2, "idle": 999_999}]},
        )
        removed = await self._janitor(redis).sweep_once()

        assert removed == 0
        redis.xgroup_delconsumer.assert_not_awaited()

    async def test_sweeps_all_groups(self):
        redis = _redis_with_groups(
            [{"name": "stats"}, {"name": "hotness"}],
            {
                "stats": [{"name": "s-old", "pending": 0, "idle": 999_999}],
                "hotness": [{"name": "h-old", "pending": 0, "idle": 999_999}],
            },
        )
        assert await self._janitor(redis).sweep_once() == 2


class TestPerGroupLines:
    @pytest.mark.asyncio
    async def test_report_once_emits_one_flat_line_per_group(self):
        redis = AsyncMock()
        redis.xlen.return_value = 7
        redis.xinfo_groups.return_value = [
            {"name": "stats", "pending": 1, "lag": 2},
            {"name": "webhooks", "pending": 0, "lag": 40},
        ]
        with capture_logs() as logs:
            await StreamMetricsReporter(redis, "events:clicks", 30).report_once()
        flat = [e for e in logs if e["event"] == "stream_group_stats"]
        assert [(e["group"], e["lag"]) for e in flat] == [
            ("stats", 2),
            ("webhooks", 40),
        ]
        assert all(e["stream"] == "events:clicks" for e in flat)


class TestWebhookDepthReporter:
    @pytest.mark.asyncio
    async def test_summary_and_per_endpoint_lines(self):
        repo = AsyncMock()
        eps = []
        for pending in (900, 12):
            ep = WebhookEndpointDoc(
                user_id=ObjectId(),
                url="https://example.com/h",
                events=["*"],
                pending_count=pending,
            )
            ep.id = ObjectId()
            eps.append(ep)
        repo.backlog_totals.return_value = (2, 912)
        repo.find_backlogged.return_value = eps
        with capture_logs() as logs:
            await WebhookDepthReporter(repo, 30).report_once()
        summary = next(e for e in logs if e["event"] == "webhook_pending_depth")
        assert summary["backlogged_endpoints"] == 2
        assert summary["total_pending"] == 912
        assert summary["max_pending"] == 900
        per_ep = [e for e in logs if e["event"] == "webhook_endpoint_depth"]
        assert [e["pending"] for e in per_ep] == [900, 12]
        assert per_ep[0]["endpoint_id"] == str(eps[0].id)

    @pytest.mark.asyncio
    async def test_summary_counts_beyond_the_top_list(self):
        """The per-endpoint lines are capped; the totals must not be."""
        repo = AsyncMock()
        top = WebhookEndpointDoc(
            user_id=ObjectId(),
            url="https://example.com/h",
            events=["*"],
            pending_count=900,
        )
        top.id = ObjectId()
        repo.backlog_totals.return_value = (2, 912)
        repo.find_backlogged.return_value = [top]
        with capture_logs() as logs:
            await WebhookDepthReporter(repo, 30, top=1).report_once()
        repo.find_backlogged.assert_awaited_once_with(limit=1)
        summary = next(e for e in logs if e["event"] == "webhook_pending_depth")
        assert summary["backlogged_endpoints"] == 2
        assert summary["total_pending"] == 912
        assert summary["max_pending"] == 900
        assert len([e for e in logs if e["event"] == "webhook_endpoint_depth"]) == 1

    @pytest.mark.asyncio
    async def test_empty_backlog_still_reports_zero(self):
        repo = AsyncMock()
        repo.backlog_totals.return_value = (0, 0)
        repo.find_backlogged.return_value = []
        with capture_logs() as logs:
            await WebhookDepthReporter(repo, 30).report_once()
        summary = next(e for e in logs if e["event"] == "webhook_pending_depth")
        assert summary["total_pending"] == 0 and summary["max_pending"] == 0


class TestWebhookDepthReporterLoop:
    @pytest.mark.asyncio
    async def test_report_error_is_logged_and_loop_keeps_going(self):
        repo = AsyncMock()
        repo.backlog_totals.side_effect = [RuntimeError("mongo down"), (0, 0)]
        repo.find_backlogged.return_value = []
        reporter = WebhookDepthReporter(repo, 0.01)
        with capture_logs() as logs:
            task = asyncio.create_task(reporter.run_forever())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert any(e["event"] == "webhook_pending_depth_failed" for e in logs)
        assert repo.backlog_totals.await_count >= 2

    @pytest.mark.asyncio
    async def test_cancel_during_report_propagates(self):
        repo = AsyncMock()
        started = asyncio.Event()

        async def hang(*, limit):
            started.set()
            await asyncio.sleep(10)

        repo.backlog_totals.return_value = (0, 0)
        repo.find_backlogged.side_effect = hang
        task = asyncio.create_task(WebhookDepthReporter(repo, 0.01).run_forever())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
