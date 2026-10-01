"""Shared helpers for the api_v1 route modules.

Small route-layer utilities used by more than one router in this package.
Lives here so route modules never reach into each other for privates.
"""

from __future__ import annotations

from bson import ObjectId

from app.errors import ValidationError


def parse_object_id(value: str, *, message: str) -> ObjectId:
    """Parse an ObjectId path param, raise 400 with *message* on bad input."""
    try:
        return ObjectId(value)
    except Exception:
        raise ValidationError(message) from None


def parse_url_id(url_id: str) -> ObjectId:
    """Parse url_id path param to ObjectId, raise 400 on invalid format."""
    return parse_object_id(url_id, message="Invalid URL ID format")
