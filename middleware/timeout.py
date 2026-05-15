"""Request deadline middleware — caps request time below the platform timeout."""

from __future__ import annotations

import asyncio

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from infrastructure.logging import get_logger
from shared.ip_utils import get_client_ip

log = get_logger("spoo.timeout")

EXEMPT_PATHS: frozenset[str] = frozenset({"/metric"})


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, timeout_seconds: float = 8.0) -> None:
        super().__init__(app)
        self._timeout = timeout_seconds

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        try:
            return await asyncio.wait_for(call_next(request), timeout=self._timeout)
        except asyncio.TimeoutError:
            log.error(
                "request_deadline_exceeded",
                method=request.method,
                path=request.url.path,
                timeout_seconds=self._timeout,
                client_ip=get_client_ip(request),
            )
            return JSONResponse(
                status_code=504,
                content={"error": "Request timed out", "code": "request_timeout"},
            )
