"""The plan a principal resolves to; the catalog keys its defaults by it."""

from enum import Enum


class Plan(str, Enum):
    ANONYMOUS = "anonymous"
    FREE = "free"
    PRO = "pro"
    SELFHOST = "selfhost"
