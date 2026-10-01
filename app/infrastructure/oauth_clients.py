"""OAuth provider strategies and Authlib client initialisation.

Relocated from utils/oauth_providers.py and utils/oauth_utils.py.
Strategy ABC + registry are unchanged. Authlib init adapted for
FastAPI/Starlette (no Flask app; clients stored on app.state).

Functions that touch the database (find_user_by_provider, create_oauth_user,
link_provider_to_user, etc.) will move to the service layer in Phase 7.
For now they remain here to keep the migration non-breaking.
"""

import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from authlib.integrations.starlette_client import OAuth

from app.infrastructure.logging import get_logger
from app.schemas.models.user import OAuthAction, ProviderInfo

log = get_logger(__name__)


# ── Provider strategies ───────────────────────────────────────────────────────


class OAuthProviderStrategy(ABC):
    """Encapsulates everything that differs between OAuth providers."""

    @property
    @abstractmethod
    def key(self) -> str: ...

    @abstractmethod
    async def fetch_user_info(self, client: Any, token: Any) -> ProviderInfo: ...


class GoogleStrategy(OAuthProviderStrategy):
    key = "google"

    async def fetch_user_info(self, client: Any, token: Any) -> ProviderInfo:
        userinfo = token.get("userinfo")
        if userinfo is None:
            resp = await client.get("userinfo", token=token)
            resp.raise_for_status()
            userinfo = resp.json()
        return extract_user_info_from_google(userinfo)


class GitHubStrategy(OAuthProviderStrategy):
    key = "github"

    async def fetch_user_info(self, client: Any, token: Any) -> ProviderInfo:
        user_response = await client.get("user", token=token)
        user_response.raise_for_status()
        user = user_response.json()
        emails_response = await client.get("user/emails", token=token)
        emails = emails_response.json() if emails_response.status_code == 200 else []
        if not isinstance(emails, list):
            emails = []
        return extract_user_info_from_github(user, emails)


class DiscordStrategy(OAuthProviderStrategy):
    key = "discord"

    async def fetch_user_info(self, client: Any, token: Any) -> ProviderInfo:
        resp = await client.get("users/@me", token=token)
        resp.raise_for_status()
        return extract_user_info_from_discord(resp.json())


PROVIDER_STRATEGIES: dict[str, OAuthProviderStrategy] = {
    s.key: s() for s in [GoogleStrategy, GitHubStrategy, DiscordStrategy]
}


# ── Authlib init ─────────────────────────────────────────────────────────────


def init_oauth(settings: Any) -> tuple[OAuth | None, dict[str, Any]]:
    """Initialise Authlib OAuth clients for FastAPI/Starlette.

    Accepts an OAuthProviderSettings instance (from config.py).
    Returns (oauth, providers_dict) — store both on app.state in create_app().
    Returns (None, {}) if no providers are configured.
    """
    oauth = OAuth()
    providers: dict[str, Any] = {}

    if settings.google_oauth_client_id and settings.google_oauth_client_secret:
        try:
            google = oauth.register(
                name="google",
                client_id=settings.google_oauth_client_id,
                client_secret=settings.google_oauth_client_secret,
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={
                    "scope": "openid email profile",
                    "prompt": "select_account",
                },
            )
            providers["google"] = google
            log.info("oauth_provider_initialized", provider="google")
        except Exception as e:
            log.error("oauth_provider_init_failed", provider="google", error=str(e))

    if settings.github_oauth_client_id and settings.github_oauth_client_secret:
        try:
            github = oauth.register(
                name="github",
                client_id=settings.github_oauth_client_id,
                client_secret=settings.github_oauth_client_secret,
                access_token_url="https://github.com/login/oauth/access_token",
                authorize_url="https://github.com/login/oauth/authorize",
                api_base_url="https://api.github.com/",
                client_kwargs={"scope": "user:email"},
            )
            providers["github"] = github
            log.info("oauth_provider_initialized", provider="github")
        except Exception as e:
            log.error("oauth_provider_init_failed", provider="github", error=str(e))

    if settings.discord_oauth_client_id and settings.discord_oauth_client_secret:
        try:
            discord = oauth.register(
                name="discord",
                client_id=settings.discord_oauth_client_id,
                client_secret=settings.discord_oauth_client_secret,
                access_token_url="https://discord.com/api/oauth2/token",
                authorize_url="https://discord.com/api/oauth2/authorize",
                api_base_url="https://discord.com/api/",
                client_kwargs={"scope": "identify email"},
            )
            providers["discord"] = discord
            log.info("oauth_provider_initialized", provider="discord")
        except Exception as e:
            log.error("oauth_provider_init_failed", provider="discord", error=str(e))

    if not providers:
        log.warning("oauth_no_providers_configured")
        return None, {}

    return oauth, providers


# ── State utilities ───────────────────────────────────────────────────────────

# How long a login/link flow may stay in flight. Long enough for a provider
# consent screen (account picker, password, 2FA), short enough that the CSRF
# state is not a durable credential. factory.py reuses this as the lifetime of the
# session cookie Authlib keeps its half of the state in, so the two halves of
# the flow expire together.
OAUTH_STATE_TTL_SECONDS = 600


def generate_oauth_state(
    provider: str,
    action: OAuthAction = OAuthAction.LOGIN,
    user_id: str | None = None,
    next_url: str | None = None,
) -> str:
    """Generate a URL-safe state string for CSRF protection."""
    action_str = action.value if isinstance(action, OAuthAction) else action
    parts = [
        f"provider={provider}",
        f"action={action_str}",
        f"nonce={secrets.token_urlsafe(32)}",
        f"timestamp={datetime.now(timezone.utc).isoformat()}",
    ]
    if user_id:
        parts.append(f"user_id={user_id}")
    if next_url:
        parts.append(f"next={next_url}")
    return "&".join(parts)


def verify_oauth_state(
    state: str, expected_provider: str
) -> tuple[bool, dict[str, Any], str | None]:
    """Verify and decode an OAuth state string.

    Returns (is_valid, state_data, failure_reason).
    failure_reason is None on success; one of "provider_mismatch",
    "missing_timestamp", "expired", or "parse_error" on failure.
    """
    try:
        state_data: dict[str, Any] = {}
        for part in state.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                state_data[key] = value

        if state_data.get("provider") != expected_provider:
            return False, {}, "provider_mismatch"

        timestamp_str = state_data.get("timestamp")
        if not timestamp_str:
            return False, {}, "missing_timestamp"

        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if age > OAUTH_STATE_TTL_SECONDS:
            return False, {}, "expired"

        return True, state_data, None
    except Exception:
        return False, {}, "parse_error"


def get_oauth_redirect_url(provider: str, settings: Any) -> str:
    """Return the OAuth redirect URI from settings, or build a default.

    Checks {PROVIDER}_OAUTH_REDIRECT_URI setting first, then falls back to
    constructing a standard callback URL. In the new FastAPI app, call this
    from the route handler where the base URL is known.
    """
    env_redirect = getattr(settings, f"{provider}_oauth_redirect_uri", "")
    if env_redirect:
        return env_redirect
    # Fallback: callers must supply base_url explicitly in the FastAPI route.
    return ""


# ── User-info extractors ──────────────────────────────────────────────────────


def extract_user_info_from_google(userinfo: dict[str, Any]) -> ProviderInfo:
    return ProviderInfo(
        provider_user_id=userinfo.get("sub", ""),
        email=userinfo.get("email", "").lower().strip(),
        email_verified=userinfo.get("email_verified", False),
        name=userinfo.get("name", ""),
        picture=userinfo.get("picture", ""),
        given_name=userinfo.get("given_name", ""),
        family_name=userinfo.get("family_name", ""),
    )


def extract_user_info_from_github(
    userinfo: dict[str, Any], email_data: list[dict[str, Any]]
) -> ProviderInfo:
    primary_email = ""
    email_verified = False
    for entry in email_data:
        if entry.get("primary", False):
            primary_email = entry.get("email", "").lower().strip()
            email_verified = entry.get("verified", False)
            break
    if not primary_email and email_data:
        primary_email = email_data[0].get("email", "").lower().strip()
        email_verified = email_data[0].get("verified", False)

    name = userinfo.get("name", "") or userinfo.get("login", "")
    return ProviderInfo(
        provider_user_id=str(userinfo.get("id", "")),
        email=primary_email,
        email_verified=email_verified,
        name=name,
        picture=userinfo.get("avatar_url", ""),
        given_name=name.split(" ")[0] if name else "",
        family_name=" ".join(name.split(" ")[1:]) if name and " " in name else "",
    )


def extract_user_info_from_discord(userinfo: dict[str, Any]) -> ProviderInfo:
    email = userinfo.get("email", "").lower().strip()
    email_verified = userinfo.get("verified", False)
    name = (
        userinfo.get("global_name")
        or userinfo.get("display_name")
        or userinfo.get("username", "")
    )
    avatar_hash = userinfo.get("avatar")
    user_id = userinfo.get("id", "")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        if avatar_hash and user_id
        else ""
    )
    return ProviderInfo(
        provider_user_id=str(userinfo.get("id", "")),
        email=email,
        email_verified=email_verified,
        name=name,
        picture=avatar_url,
        given_name=name.split(" ")[0] if name and " " in name else name,
        family_name=" ".join(name.split(" ")[1:]) if name and " " in name else "",
    )
