#!/usr/bin/env -S uv run --script

# PEP 723 metadata so this runs standalone via `uv run --script` (keep)
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymongo>=4.6",
# ]
# ///
"""One-shot migration: remove the retired ``users.plan`` field.

The plan is now the status on the user's ``subscriptions`` document; a
stored ``plan`` on the user is dead data that nothing reads. Also clears
the retired ``tier`` field and ``rollout_type: "tier"`` on feature flags,
which the app no longer recognises.

Reads ``MONGODB_URI`` and (optional) ``DB_NAME`` from the environment.

Usage::

    uv run --env-file .env.production scripts/drop_users_plan.py
    uv run --env-file .env.production scripts/drop_users_plan.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

from pymongo import MongoClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("MONGODB_URI is not set", file=sys.stderr)
        return 2
    db = MongoClient(uri)[os.environ.get("DB_NAME", "url-shortener")]

    users_with_plan = db["users"].count_documents({"plan": {"$exists": True}})
    flags_with_tier = db["feature_flags"].count_documents(
        {"$or": [{"tier": {"$exists": True}}, {"rollout_type": "tier"}]}
    )
    print(f"users with plan: {users_with_plan}, flags with tier: {flags_with_tier}")
    if args.dry_run:
        return 0

    r1 = db["users"].update_many({"plan": {"$exists": True}}, {"$unset": {"plan": ""}})
    r2 = db["feature_flags"].update_many(
        {"tier": {"$exists": True}}, {"$unset": {"tier": ""}}
    )
    r3 = db["feature_flags"].update_many(
        {"rollout_type": "tier"}, {"$set": {"rollout_type": "off"}}
    )
    print(
        f"users updated: {r1.modified_count}, flags untiered: {r2.modified_count}, "
        f"tier rollouts turned off: {r3.modified_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
