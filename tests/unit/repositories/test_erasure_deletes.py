"""Account-erasure bulk deletes on the satellite repositories.

Each is a one-line ``_delete_many`` delegate; what matters is the filter
key — a wrong key silently erases nothing (or worse, everything).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.page_layout_repository import PageLayoutRepository
from app.repositories.webhook_delivery_repository import WebhookDeliveryRepository
from app.repositories.webhook_endpoint_repository import WebhookEndpointRepository
from app.repositories.webhook_event_repository import WebhookEventRepository

from .conftest import USER_OID, make_collection


@pytest.mark.parametrize(
    ("repo_cls", "method", "filter_key"),
    [
        (PageLayoutRepository, "delete_by_user", "user_id"),
        (WebhookEndpointRepository, "delete_by_user", "user_id"),
        (WebhookDeliveryRepository, "delete_by_user", "user_id"),
        (WebhookEventRepository, "delete_by_owner", "owner_id"),
    ],
)
@pytest.mark.asyncio
async def test_erasure_delete_filters_on_the_user(repo_cls, method, filter_key):
    col = make_collection()
    col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=5))
    count = await getattr(repo_cls(col), method)(USER_OID)
    col.delete_many.assert_awaited_once_with({filter_key: USER_OID})
    assert count == 5
