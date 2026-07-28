#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "redis>=5",
# ]
# ///
"""Replay dead-lettered click events back onto the main stream.

Standalone — reads ``CLICK_EVENTS_QUEUE_REDIS_URI`` from the environment.
Replayed events fan out to every consumer group again (streams have no
per-group publish); replayed entries leave the DLQ unless ``--keep``.

Usage::

    uv run --env-file .env.production scripts/replay_dlq.py --dry-run
    uv run --env-file .env.production scripts/replay_dlq.py --limit 500 --keep
"""

from __future__ import annotations

import argparse
import os
import sys

from redis import Redis

STREAM_FIELD_DATA = "__data__"  # payload field the workers' parser consumes
DLQ_FIELD_SOURCE_ID = "dlq_source_id"
DLQ_FIELD_GROUP = "dlq_group"
DLQ_FIELD_REASON = "dlq_reason"

_BATCH = 200


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be replayed without writing.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Replay at most N entries (0 = all)."
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete replayed entries from the DLQ.",
    )
    parser.add_argument("--stream", default="events:clicks")
    parser.add_argument("--dlq-stream", default="events:clicks:dlq")
    args = parser.parse_args()

    uri = os.environ.get("CLICK_EVENTS_QUEUE_REDIS_URI")
    if not uri:
        sys.exit("CLICK_EVENTS_QUEUE_REDIS_URI not set in environment.")

    redis = Redis.from_url(uri, decode_responses=True)
    total = redis.xlen(args.dlq_stream)
    print(f"DLQ {args.dlq_stream}: {total} entries")
    if total == 0:
        return

    replayed = 0
    by_group: dict[str, int] = {}
    skipped = 0
    cursor = "-"
    while True:
        entries = redis.xrange(args.dlq_stream, min=cursor, max="+", count=_BATCH)
        if not entries:
            break
        for entry_id, fields in entries:
            if args.limit and replayed >= args.limit:
                break
            data = fields.get(STREAM_FIELD_DATA)
            if data is None:
                skipped += 1
                continue
            group = fields.get(DLQ_FIELD_GROUP, "?")
            if args.dry_run:
                print(
                    f"  would replay {entry_id} "
                    f"(source={fields.get(DLQ_FIELD_SOURCE_ID, '?')}, "
                    f"group={group}, reason={fields.get(DLQ_FIELD_REASON, '?')})"
                )
            else:
                redis.xadd(args.stream, {STREAM_FIELD_DATA: data})
                if not args.keep:
                    redis.xdel(args.dlq_stream, entry_id)
            replayed += 1
            by_group[group] = by_group.get(group, 0) + 1
        if (args.limit and replayed >= args.limit) or len(entries) < _BATCH:
            break
        # xrange min is inclusive — nudge past the last-seen id
        cursor = f"({entries[-1][0]}"

    verb = "would replay" if args.dry_run else "replayed"
    print(
        f"{verb} {replayed} (skipped {skipped} without payload) — by group: {by_group}"
    )


if __name__ == "__main__":
    main()
