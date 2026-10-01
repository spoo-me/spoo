"""
Shared fixtures for unit route tests.
"""

from __future__ import annotations

import pytest

from app.middleware.rate_limiter import limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the in-memory rate limiter before and after every test.

    The slowapi limiter is a module-level singleton with in-memory storage
    during tests. Without this reset, rate limit counters leak across test
    files and cause spurious 429 failures — the deletion endpoints ride a
    3-per-hour budget, so even one file would trip it.
    """
    limiter.reset()
    yield
    limiter.reset()
