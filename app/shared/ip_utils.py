"""
Client IP resolution for FastAPI requests.

Replaces the Flask-global version in utils/url_utils.py with an explicit
``Request`` parameter so the function is testable without a request context.
"""

from __future__ import annotations

from fastapi import Request


def get_client_ip(request: Request) -> str:
    """Extract the real client IP from a FastAPI ``Request``.

    Checks proxy headers in priority order before falling back to the
    direct connection address:

    1. ``CF-Connecting-IP`` — Cloudflare
    2. ``True-Client-IP`` — Akamai and others
    3. ``X-Forwarded-For`` — standard proxy header (first IP in list)
    4. ``X-Real-IP`` — nginx / other reverse proxies
    5. ``X-Client-IP`` — less common

    Args:
        request: The current FastAPI ``Request`` object.

    Returns:
        The resolved client IP string, or ``""`` if none can be found.
    """
    headers_to_check: list[str] = [
        "CF-Connecting-IP",
        "True-Client-IP",
        "X-Forwarded-For",
        "X-Real-IP",
        "X-Client-IP",
    ]

    for header in headers_to_check:
        ip_value: str | None = request.headers.get(header)
        if ip_value:
            client_ip: str = ip_value.split(",")[0].strip()
            if client_ip:
                return client_ip

    return request.client.host if request.client else ""
