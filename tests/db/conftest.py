"""Real-MongoDB fixtures for the tests/db suite.

Every test here runs real predicates against real documents — no mocks
between the query and the server. Each test gets a uniquely named throwaway
database on MONGO_TEST_URI with the repo's actual ``ensure_indexes`` applied,
so partial indexes, the time-series clicks collection, and the guarded
claim/restore updates behave exactly as production.

The database is per-test rather than per-session because pymongo's async
client is bound to the event loop it was created on and pytest-asyncio gives
each test a fresh function-scoped loop; per-test also guarantees isolation
under xdist. Index creation against a local server costs well under a second.

Availability policy: unreachable Mongo skips the suite locally (dev without
the docker stack) but fails loudly when ``CI`` is set — a missing service
container must never silently skip the only real-database coverage.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from pymongo import MongoClient
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from app.config import AppSettings
from app.dependencies.wiring import build_account_erasure_service
from app.repositories.indexes import ensure_indexes
from app.repositories.user_repository import UserRepository

MONGO_TEST_URI = os.environ.get(
    "MONGO_TEST_URI", "mongodb://localhost:27017/?directConnection=true"
)

# None = unprobed, "" = reachable, anything else = the probe error.
_probe_result: str | None = None


def _mongo_error() -> str:
    global _probe_result
    if _probe_result is None:
        try:
            probe: MongoClient = MongoClient(
                MONGO_TEST_URI, serverSelectionTimeoutMS=2000
            )
            # Not ping: auth-required servers answer ping unauthenticated,
            # and a probe that passes while index creation would 13 is a lie.
            probe.list_database_names()
            probe.close()
            _probe_result = ""
        except Exception as exc:
            _probe_result = f"{type(exc).__name__}: {exc}"
    return _probe_result


@pytest.fixture
async def real_db():
    error = _mongo_error()
    if error:
        if os.environ.get("CI"):
            pytest.fail(
                f"tests/db requires MongoDB at {MONGO_TEST_URI} and CI is set — "
                f"the mongo service container is missing or unhealthy ({error})"
            )
        pytest.skip(
            f"MongoDB not reachable at {MONGO_TEST_URI} ({error}) — start the "
            "local stack (docker compose up db) or point MONGO_TEST_URI at one"
        )
    client: AsyncMongoClient = AsyncMongoClient(MONGO_TEST_URI)
    name = f"dbtest_{uuid.uuid4().hex}"
    db = client[name]
    await ensure_indexes(db)
    try:
        yield db
    finally:
        await client.drop_database(name)
        await client.close()


@pytest.fixture
def erasure_service(real_db, monkeypatch):
    """The worker's real erasure composition over the throwaway db.

    ``build_account_erasure_service`` is the production dependency graph:
    real repositories under a real UrlService and CustomDomainService. The
    mock DCV backend keeps the domain cascade Cloudflare-free; edge cache,
    R2, PostHog, and mail stay unconfigured and wire to their Noop/None
    forms, and a None redis degrades cache invalidation to no-ops.
    """
    monkeypatch.setenv("CUSTOM_DOMAINS_MOCK_DCV", "true")
    return build_account_erasure_service(
        real_db, AppSettings(), http_client=MagicMock(), redis_client=None
    )


@pytest.fixture
def user_repo(real_db) -> UserRepository:
    return UserRepository(real_db["users"])
