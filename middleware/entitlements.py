"""Inject ``X-Entitlements-Version`` on every authenticated response.

The ``Entitled`` dependency records the version it resolved on the request
state; routes that never resolved it get one cache read here. Clients
compare the header with the version they hold and refetch on a change, so
an override, lapse, or payment lands on the user's very next request.
Header failures are swallowed: the header is advisory and must never fail a
response whose work already completed.
"""

from __future__ import annotations

from bson import ObjectId
from starlette.datastructures import MutableHeaders

from infrastructure.logging import get_logger

log = get_logger(__name__)

HEADER = "X-Entitlements-Version"


class EntitlementsVersionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                version = await self._version(scope)
                if version is not None:
                    MutableHeaders(raw=message["headers"])[HEADER] = str(version)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _version(scope) -> int | None:
        state = scope.get("state", {})
        version = state.get("entitlements_version")
        if version is not None:
            return version
        auth_ctx = state.get("auth_ctx")
        if not auth_ctx:
            return None
        service = getattr(scope["app"].state, "entitlement_service", None)
        if service is None:
            return None
        try:
            return await service.version_for(ObjectId(auth_ctx["user_id"]))
        except Exception:
            log.warning("entitlements_version_header_failed", exc_info=True)
            return None
