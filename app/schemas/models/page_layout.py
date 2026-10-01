"""
Document model for the `page-layouts` collection.

One document per (user, page): an opaque, client-owned dashboard layout doc.
The server never interprets the layout — schema and versioning live in the
frontend; absence of a document means "use the client's default layout".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.models.base import MongoBaseModel, PyObjectId


class PageLayoutDoc(MongoBaseModel):
    user_id: PyObjectId
    page: str
    layout: dict[str, Any]
    updated_at: datetime | None = None
