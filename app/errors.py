"""
Application error hierarchy.

AppError is the base for all typed errors.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import NotRequired, TypedDict

# Wire shape of a JSON error body. ``code`` is the machine-readable slug
# that also feeds the ``X-Error-Code`` response header, so a body without
# it is a type error at the call site rather than a KeyError inside an
# exception handler.
#
# Functional syntax on purpose: under ``from __future__ import annotations``
# the class form stringifies its annotations and the (Not)Required markers
# are silently dropped at class creation — ``__required_keys__`` comes out
# empty. Dict-literal values are real objects, so the markers survive.
ErrorBody = TypedDict(  # noqa: UP013 — class syntax loses the markers here
    "ErrorBody",
    {
        "error": str,
        "code": str,
        "field": NotRequired[str],
        "details": NotRequired[Any],
        "message": NotRequired[str],
        "hint": NotRequired[str],
    },
)


class AppError(Exception):
    """Base application error. All typed errors inherit from this."""

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details

    def to_dict(self) -> ErrorBody:
        payload: ErrorBody = {"error": self.message, "code": self.error_code}
        if self.field is not None:
            payload["field"] = self.field
        if self.details is not None:
            payload["details"] = self.details
        return payload


class ValidationError(AppError):
    status_code = 400
    error_code = "validation_error"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "authentication_error"


class PasswordRequiredError(AuthenticationError):
    """A password-protected resource was requested without a password."""

    error_code = "password_required"


class InvalidPasswordError(AuthenticationError):
    """A password-protected resource was requested with the wrong password."""

    error_code = "invalid_password"


class ForbiddenError(AppError):
    status_code = 403
    error_code = "forbidden"


class EmailNotVerifiedError(ForbiddenError):
    """Raised when email verification is required before proceeding."""

    error_code = "EMAIL_NOT_VERIFIED"

    def to_dict(self) -> ErrorBody:
        d = super().to_dict()
        d["message"] = (
            "You must verify your email address before creating resources. "
            "Check your inbox for the verification code."
        )
        return d


class AccountPendingDeletionError(ForbiddenError):
    """Login attempted against an account scheduled for erasure.

    The uppercase slug rides the ``X-Error-Code`` header (error-pages
    contract), so the frontend can branch to the restore flow instead of
    rendering a generic 403.
    """

    error_code = "ACCOUNT_PENDING_DELETION"


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"


class BlockedUrlError(AppError):
    status_code = 451
    error_code = "blocked"


class NotYetLiveError(AppError):
    """A scheduled link was requested before its start time. Answers 404
    (nothing is reachable yet); ``fallback_url`` is the owner's pre-start
    destination when they set one."""

    status_code = 404
    error_code = "not_yet_live"

    def __init__(self, message: str, *, fallback_url: str | None = None) -> None:
        super().__init__(message)
        self.fallback_url = fallback_url


class GoneError(AppError):
    status_code = 410
    error_code = "gone"


class ExpiredRedirectError(GoneError):
    """Expired link whose owner set a fallback destination."""

    def __init__(self, redirect_url: str) -> None:
        super().__init__("URL has expired")
        self.redirect_url = redirect_url


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limit_exceeded"


class NotConfiguredError(AppError):
    """An optional feature's required config (e.g. a webhook URL) is unset
    on this instance — the surface exists but cannot operate.

    JSON twin of the Jinja contact/report handlers' 503 error page."""

    status_code = 503
    error_code = "not_configured"


class R2StorageError(AppError):
    """R2 object PUT failed — the user write that needed it must fail
    loudly rather than store a broken image URL."""

    status_code = 502
    error_code = "storage_error"


# ── Custom-domain errors ─────────────────────────────────────────────────


class DomainAlreadyRegisteredError(ConflictError):
    """The fqdn is already registered (by this user or another)."""

    error_code = "domain_already_registered"


class DomainNotVerifiedError(ValidationError):
    """Operation requires the domain to be in ACTIVE status."""

    status_code = 422
    error_code = "domain_not_verified"


class DomainBlocklistedError(ValidationError):
    """The fqdn matches a blocklisted name (Tranco top-N, abuse list, etc.)."""

    status_code = 422
    error_code = "domain_blocklisted"


class DomainQuotaExceededError(RateLimitError):
    """Per-user max-domains or per-window create/verify limit hit."""

    error_code = "domain_quota_exceeded"


class InvalidDomainTransitionError(ValidationError):
    """Requested status transition isn't legal — see LEGAL_TRANSITIONS."""

    status_code = 422
    error_code = "invalid_domain_transition"


class FeatureDisabledError(AppError):
    """Feature flag is off / config missing; the feature isn't available.

    Returns 404 to match the route-layer's existing "hide the feature
    from non-allowlisted users" posture. 429 (the previous behavior via
    DomainQuotaExceededError) misled clients into retrying — retry won't
    help when a feature is simply off.
    """

    status_code = 404
    error_code = "feature_disabled"

    def __init__(self, feature: str, message: str | None = None) -> None:
        self.feature = feature
        super().__init__(message or f"The {feature} feature is not available.")


class CloudflareAPIError(AppError):
    """Cloudflare API call failed (4xx, 5xx, or network).

    Raw CF response bodies are intentionally stripped from the API
    response by ``to_dict()`` — they can carry zone IDs, internal account
    metadata, or token-scoped messages that have no business reaching
    the API caller. The underlying ``details`` payload stays on the
    exception instance for upstream logging.
    """

    status_code = 502
    error_code = "cloudflare_api_error"

    def to_dict(self) -> ErrorBody:
        return {
            "error": (
                "Upstream certificate service is temporarily unavailable. "
                "Please try again in a moment."
            ),
            "code": self.error_code,
        }


class CloudflareNotConfiguredError(AppError):
    """CF SaaS path invoked but settings missing — wiring bug."""

    status_code = 500
    error_code = "cloudflare_not_configured"
