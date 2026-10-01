"""
Shared fixtures for all integration tests.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from app.middleware.rate_limiter import limiter


@pytest.fixture
def edge_composed_errors():
    """Flip EDGE_COMPOSED_ERRORS on for apps built inside the test.

    Every test app builder constructs ``AppSettings()`` at call time, so
    patching the env for the test's duration is enough — no lifespan surgery
    needed.
    """
    with patch.dict(os.environ, {"EDGE_COMPOSED_ERRORS": "true"}, clear=False):
        yield


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the in-memory rate limiter before and after every integration test.

    The slowapi limiter is a module-level singleton with in-memory storage
    during tests. Without this reset, rate limit counters leak across test
    files and cause spurious 429 failures.
    """
    limiter.reset()
    yield
    limiter.reset()
