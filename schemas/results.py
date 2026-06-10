"""
Named result types for service operations.

Frozen value objects that replace positional tuples where index-based
access is error-prone.  Each field is named and typed, making call-site
code self-documenting:  ``result.access_token`` instead of ``tokens[1]``.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemas.models.user import UserDoc


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Result of an authentication operation (login, register, refresh, etc.).

    All auth flows that produce a user + token pair return this type.
    """

    user: UserDoc
    access_token: str
    refresh_token: str
    verification_sent: bool = False
    app_id: str | None = None
    is_new: bool = False


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Result of a data export operation.

    Contains the serialized bytes, MIME type for the HTTP response, and
    a suggested filename for the Content-Disposition header.
    """

    content: bytes
    mimetype: str
    filename: str
