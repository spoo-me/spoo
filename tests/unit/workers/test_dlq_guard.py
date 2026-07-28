"""Tests for the claim-path dead-letter guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

from workers.dlq import (
    DLQ_FIELD_GROUP,
    DLQ_FIELD_REASON,
    DLQ_FIELD_SOURCE_ID,
    DLQ_MAXLEN,
    ClaimDeadLetterGuard,
)


def _guard(max_deliveries: int = 5) -> ClaimDeadLetterGuard:
    return ClaimDeadLetterGuard(
        stream="events:clicks",
        group="stats",
        dlq_stream="events:clicks:dlq",
        max_deliveries=max_deliveries,
    )


def _redis_with_deliveries(times: int) -> AsyncMock:
    redis = AsyncMock()
    redis.xpending_range.return_value = [
        {
            "message_id": "5-0",
            "consumer": "dead-consumer",
            "time_since_delivered": 70_000,
            "times_delivered": times,
        }
    ]
    return redis


class TestClaimDeadLetterGuard:
    async def test_within_delivery_budget_is_not_intercepted(self):
        redis = _redis_with_deliveries(times=5)  # == max, still allowed
        assert await _guard(5).intercept(redis, "5-0", {"x": 1}) is False
        redis.xadd.assert_not_awaited()

    async def test_past_budget_moves_to_dlq(self):
        redis = _redis_with_deliveries(times=6)
        intercepted = await _guard(5).intercept(redis, "5-0", {"short_code": "abc"})

        assert intercepted is True
        redis.xpending_range.assert_awaited_once_with(
            "events:clicks", "stats", min="5-0", max="5-0", count=1
        )
        args = redis.xadd.await_args.args
        assert args[0] == "events:clicks:dlq"
        fields = args[1]
        assert fields[DLQ_FIELD_SOURCE_ID] == "5-0"
        assert fields[DLQ_FIELD_GROUP] == "stats"
        assert fields[DLQ_FIELD_REASON] == "max_deliveries_exceeded"
        assert "abc" in fields["__data__"]
        # DLQ must stay bounded — queue Redis is noeviction
        kwargs = redis.xadd.await_args.kwargs
        assert kwargs["maxlen"] == DLQ_MAXLEN
        assert kwargs["approximate"] is True

    async def test_custom_dlq_maxlen_is_used(self):
        redis = _redis_with_deliveries(times=6)
        guard = ClaimDeadLetterGuard(
            stream="events:clicks",
            group="stats",
            dlq_stream="events:clicks:dlq",
            max_deliveries=5,
            dlq_maxlen=100_000,
        )
        assert await guard.intercept(redis, "5-0", {}) is True
        assert redis.xadd.await_args.kwargs["maxlen"] == 100_000

    async def test_bytes_delivery_metadata_is_handled(self):
        """The broker's internal client speaks bytes."""
        redis = AsyncMock()
        redis.xpending_range.return_value = [
            {
                "message_id": b"5-0",
                "consumer": b"dead",
                "time_since_delivered": 70_000,
                "times_delivered": 7,
            }
        ]
        assert await _guard(5).intercept(redis, "5-0", {}) is True

    async def test_fails_open_when_pending_lookup_fails(self):
        redis = AsyncMock()
        redis.xpending_range.side_effect = ConnectionError("blip")
        assert await _guard().intercept(redis, "5-0", {}) is False
        redis.xadd.assert_not_awaited()

    async def test_fails_open_when_message_no_longer_pending(self):
        """Another consumer may have acked meanwhile — process normally."""
        redis = AsyncMock()
        redis.xpending_range.return_value = []
        assert await _guard().intercept(redis, "5-0", {}) is False

    async def test_fails_open_when_dlq_write_fails(self):
        """Better one more processing attempt than a lost event."""
        redis = _redis_with_deliveries(times=99)
        redis.xadd.side_effect = ConnectionError("dlq write failed")
        assert await _guard().intercept(redis, "5-0", {}) is False

    async def test_non_json_payload_still_dead_letters(self):
        """default=str keeps exotic payloads serializable for the DLQ."""
        redis = _redis_with_deliveries(times=99)
        assert await _guard().intercept(redis, "5-0", object()) is True
