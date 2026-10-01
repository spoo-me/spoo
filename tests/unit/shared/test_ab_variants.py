"""Weighted pick for A/B split links."""

from __future__ import annotations

import pytest

from app.schemas.models.url import AbVariant
from app.shared.ab_variants import pick_variant

VARIANTS = [
    AbVariant(url="https://example.com/b", weight=60),
    AbVariant(url="https://example.com/c", weight=30),
]


@pytest.mark.parametrize(
    ("roll", "expected"),
    [(0, 0), (59, 0), (60, 1), (89, 1), (90, None), (99, None)],
)
def test_bands_follow_list_order_and_remainder_is_default(roll, expected):
    assert pick_variant(VARIANTS, roll) == expected


def test_every_roll_lands_in_proportion():
    picks = [pick_variant(VARIANTS, roll) for roll in range(100)]
    assert picks.count(0) == 60
    assert picks.count(1) == 30
    assert picks.count(None) == 10


def test_full_hundred_never_falls_to_default():
    full = [AbVariant(url="https://example.com/b", weight=100)]
    assert all(pick_variant(full, roll) == 0 for roll in range(100))
