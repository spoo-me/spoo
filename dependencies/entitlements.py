"""
``Entitled``: the resolved entitlements of the request's principal.

Runs the resolver once per request (cache hit in the common case) and hands
the map to routes and services. Nothing on the request path reads
``subscriptions`` or overrides directly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from dependencies.auth import CurrentUser, get_current_user
from services.entitlements import EntitlementService, Resolved


def get_entitlement_service(request: Request) -> EntitlementService:
    return request.app.state.entitlement_service


async def get_entitlements(
    request: Request,
    user: CurrentUser | None = Depends(get_current_user),
    service: EntitlementService = Depends(get_entitlement_service),
) -> Resolved:
    resolved = await service.resolve_for(
        user.user_id if user else None,
        plan_hint=user.plan_claim if user else None,
    )
    if user is not None and not resolved.degraded:
        request.state.entitlements_version = resolved.version
    return resolved


Entitled = Annotated[Resolved, Depends(get_entitlements)]
EntitlementSvc = Annotated[EntitlementService, Depends(get_entitlement_service)]
