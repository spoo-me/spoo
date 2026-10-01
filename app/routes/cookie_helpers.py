"""
Shared cookie helpers for auth and OAuth routes.

Both routes set/clear the same access_token and refresh_token cookies
with identical parameters.  Centralising here avoids duplication.
"""

from __future__ import annotations

from fastapi import Response

from app.config import JWTSettings


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    jwt_cfg: JWTSettings,
) -> None:
    """Write access_token and refresh_token cookies onto *response*."""
    response.set_cookie(
        "access_token",
        value=access_token,
        httponly=True,
        secure=jwt_cfg.cookie_secure,
        samesite="lax",
        path="/",
        max_age=jwt_cfg.access_token_ttl_seconds,
    )
    response.set_cookie(
        "refresh_token",
        value=refresh_token,
        httponly=True,
        secure=jwt_cfg.cookie_secure,
        samesite="lax",
        path="/",
        max_age=jwt_cfg.refresh_token_ttl_seconds,
    )


def clear_auth_cookies(response: Response, jwt_cfg: JWTSettings) -> None:
    """Expire (clear) access_token and refresh_token cookies on *response*."""
    response.set_cookie(
        "access_token",
        value="",
        httponly=True,
        secure=jwt_cfg.cookie_secure,
        samesite="lax",
        path="/",
        max_age=0,
    )
    response.set_cookie(
        "refresh_token",
        value="",
        httponly=True,
        secure=jwt_cfg.cookie_secure,
        samesite="lax",
        path="/",
        max_age=0,
    )
