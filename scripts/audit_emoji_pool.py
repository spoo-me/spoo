#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     # MUST track the resolved version in uv.lock — the whole point of
#     # this script is inspecting the exact emoji dataset the app runs.
#     "emoji==2.15.0",
#     "regex>=2024.0.0",
# ]
# ///
"""Read-only inspection of the emoji-alias generation pool.

The pool is derived at import time from the pinned ``emoji`` package data
(``app/shared/emoji_policy.py``) rather than checked in as an artifact, so this
script is the reviewer tool for seeing what a given version cap actually
yields — pool size, Unicode emoji-version histogram, and a paranoia check
that every pool entry passes the acceptance policy (the same invariant the
unit tests pin).

Run from the repo root::

    uv run scripts/audit_emoji_pool.py
    uv run scripts/audit_emoji_pool.py --max-version 15.1
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import emoji

from app.shared.emoji_policy import (
    DEFAULT_GENERATE_MAX_VERSION,
    check_emoji_alias,
    generation_pool,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-version",
        type=float,
        default=DEFAULT_GENERATE_MAX_VERSION,
        help=f"Emoji version cap for the pool (default {DEFAULT_GENERATE_MAX_VERSION})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print every emoji in the pool",
    )
    args = parser.parse_args()

    pool = generation_pool(args.max_version)
    versions = Counter(emoji.EMOJI_DATA[e]["E"] for e in pool)

    print(f"pool size (E <= {args.max_version}): {len(pool)}")
    print("version histogram:")
    for version, count in sorted(versions.items()):
        print(f"  E{version:>5}: {count}")

    violations = [e for e in pool if check_emoji_alias(e) != "ok"]
    if violations:
        print(f"\nPOLICY VIOLATIONS ({len(violations)}): {''.join(violations)}")
        return 1
    print("\nall pool entries pass check_emoji_alias ✓")

    if args.list:
        print("".join(pool))
    return 0


if __name__ == "__main__":
    sys.exit(main())
