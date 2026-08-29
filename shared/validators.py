"""
URL and input validators — framework-agnostic, pure functions.

All validators are stateless; any external data (e.g. blocked URL patterns)
must be passed in as arguments so the service/repository layer controls
caching and DB access.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import unquote, urlparse

import emoji
import regex
import validators as _validators

from infrastructure.logging import get_logger

log = get_logger(__name__)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def validate_url(
    url: str,
    blocked_self_domains: Sequence[str] = ("spoo.me",),
) -> bool:
    """Return True if *url* is a valid, non-self-referential http(s) URL.

    Args:
        url: The URL string to validate.
        blocked_self_domains: Bare hostnames whose substring presence in the
            URL marks it as self-referential. Defaults to ``("spoo.me",)``
            to prevent redirect loops.
    """
    # Scheme allowlist defends against the validators package widening upstream;
    # urlparse raises on malformed bracket hosts ("https://x]:80/") — not a 500.
    try:
        scheme = urlparse(url).scheme
    except ValueError:
        return False
    if scheme not in _ALLOWED_URL_SCHEMES:
        return False
    if not _validators.url(url, skip_ipv4_addr=True, skip_ipv6_addr=True):
        return False
    url_lower = url.lower()
    return not any(domain in url_lower for domain in blocked_self_domains)


def validate_url_password(password: str, min_length: int = 8) -> bool:
    """Validate a URL password.

    Rules:
    - At least *min_length* characters (default 8)
    - Contains at least one letter
    - Contains at least one digit
    - Contains at least one of ``@`` or ``.``
    - No two consecutive special characters (``@@``, ``..``, ``@.``, ``.@``)

    Returns:
        True if the password meets all requirements.
    """
    if len(password) < min_length:
        return False
    if not re.search(r"[a-zA-Z]", password):
        return False
    if not re.search(r"\d", password):
        return False
    if not re.search(r"[@.]", password):
        return False
    return not re.search(r"[@.]{2}", password)


def validate_alias(alias: str) -> bool:
    """Return True if *alias* contains only alphanumeric characters, ``-``, or ``_``."""
    return bool(re.search(r"^[a-zA-Z0-9_-]*$", alias))


def is_emoji_alias(alias: str) -> bool:
    """Return True if *alias* consists entirely of emoji characters.

    Deliberately lenient (no length cap, no policy): used for URL
    resolution/dispatch where any previously-created emoji alias must
    stay routable regardless of the current creation policy. Creation
    validation lives in ``shared.emoji_policy``.
    """
    decoded = unquote(alias)
    if not decoded:
        return False
    emoji_list = emoji.emoji_list(decoded)
    extracted = "".join([data["emoji"] for data in emoji_list])
    return extracted == decoded


def validate_account_password(
    password: str,
    min_length: int = 8,
    max_length: int = 128,
) -> tuple[bool, list[str], int]:
    """Validate a user account password.

    Args:
        password:   The password to validate.
        min_length: Minimum required length (default 8).
        max_length: Maximum allowed length (default 128).

    Returns:
        (is_valid, missing_requirements, strength_score)
    """
    if not password:
        return False, ["Password is required"], 0

    missing: list[str] = []
    strength_score = 0

    if len(password) < min_length:
        missing.append(f"At least {min_length} characters")
    else:
        strength_score += 20

    if len(password) > max_length:
        missing.append(f"Maximum {max_length} characters")
    else:
        strength_score += 10

    if not re.search(r"[A-Z]", password):
        missing.append("At least one uppercase letter")
    else:
        strength_score += 15

    if not re.search(r"[a-z]", password):
        missing.append("At least one lowercase letter")
    else:
        strength_score += 15

    if not re.search(r"[0-9]", password):
        missing.append("At least one number")
    else:
        strength_score += 15

    if not re.search(r'[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?~`]', password):
        missing.append("At least one special character")
    else:
        strength_score += 15

    if not re.match(
        r'^[a-zA-Z0-9!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?~`\s]+$', password
    ):
        missing.append("Contains invalid characters")
    else:
        strength_score += 10

    if len(password) >= 12:
        strength_score += 5
    if len(password) >= 16:
        strength_score += 5

    if re.search(r"(.)\1{2,}", password):
        strength_score -= 10

    if re.search(
        r"(012|123|234|345|456|567|678|789|890|abc|bcd|cde|def)", password.lower()
    ):
        strength_score -= 15

    for pattern in [r"password", r"123456", r"qwerty", r"admin", r"login", r"welcome"]:
        if re.search(pattern, password.lower()):
            strength_score -= 20
            break

    return len(missing) == 0, missing, strength_score


def validate_blocked_url(
    url: str, patterns: Sequence[str], timeout: float = 0.2
) -> bool:
    """Return True if *url* does NOT match any blocked pattern.

    This is a pure function — the caller is responsible for providing the
    list of blocked URL regex patterns (typically loaded from the database
    and cached by the repository layer).

    Uses the ``regex`` library with a per-pattern timeout to prevent ReDoS
    attacks on user-supplied patterns.

    Args:
        url:      The URL to check.
        patterns: Iterable of regex pattern strings to match against.
        timeout:  Per-pattern regex timeout in seconds (default 0.2).

    Returns:
        True if the URL is allowed (no pattern matched), False if blocked.
    """
    return matching_blocked_pattern(url, patterns, timeout=timeout) is None


def is_valid_pattern(pattern: str) -> bool:
    """Whether *pattern* compiles. Model- and operator-supplied regexes are
    never handed to the matcher unchecked: a bad one raises at enforcement
    time, after the verdict is already stored."""
    if not pattern:
        return False
    try:
        regex.compile(pattern)
    except Exception:
        return False
    return True


def matching_blocked_pattern(
    url: str, patterns: Sequence[str], timeout: float = 0.2
) -> str | None:
    """Return the first blocked pattern that matches *url*, or None; safety
    enforcement scopes its action to that pattern."""
    for pattern in patterns:
        try:
            if regex.search(pattern, url, timeout=timeout):
                return pattern
        except regex.error:
            log.warning("blocked_pattern_invalid", pattern=pattern)
        except TimeoutError:
            # Fail open (a pathological pattern must not take down link
            # creation) but never silently: a timing-out pattern is an
            # operator blocklist entry that has stopped enforcing.
            log.warning(
                "blocked_pattern_timeout", pattern=pattern, timeout_seconds=timeout
            )
    return None


def validate_safe_redirect(url: str, fallback: str = "/dashboard") -> str:
    """Return *url* if it's a safe relative path, otherwise *fallback*.

    Only allows paths starting with ``/`` that don't redirect to an external
    host. Blocks ``//evil.com``, ``/\\evil.com``, and scheme-prefixed URLs.
    """
    if not url or not url.startswith("/"):
        return fallback
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return fallback
    return url
