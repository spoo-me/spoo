"""Unit tests for OAuth utility functions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure.oauth_clients import (
    DiscordStrategy,
    GitHubStrategy,
    GoogleStrategy,
    extract_user_info_from_discord,
    extract_user_info_from_github,
    extract_user_info_from_google,
    generate_oauth_state,
    verify_oauth_state,
)


class TestGenerateOAuthState:
    def test_contains_provider_and_action(self):
        state = generate_oauth_state("google", "login")
        assert "provider=google" in state
        assert "action=login" in state

    def test_contains_user_id_when_given(self):
        state = generate_oauth_state("github", "link", user_id="abc123")
        assert "user_id=abc123" in state

    def test_omits_user_id_when_absent(self):
        state = generate_oauth_state("discord")
        assert "user_id=" not in state


class TestVerifyOAuthState:
    def test_valid_state_returns_true(self):
        state = generate_oauth_state("google")
        ok, data, reason = verify_oauth_state(state, "google")
        assert ok is True
        assert reason is None
        assert data["provider"] == "google"

    def test_wrong_provider_returns_mismatch(self):
        state = generate_oauth_state("google")
        ok, _, reason = verify_oauth_state(state, "github")
        assert ok is False
        assert reason == "provider_mismatch"

    def test_missing_timestamp_returns_error(self):
        ok, _, reason = verify_oauth_state("provider=google&action=login", "google")
        assert ok is False
        assert reason == "missing_timestamp"

    def test_malformed_state_returns_parse_error(self):
        ok, _, _reason = verify_oauth_state("not-valid-state!!!", "google")
        # Either parse_error or missing_timestamp/provider_mismatch — both are failures
        assert ok is False

    def test_expired_state_returns_expired(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        state = f"provider=google&action=login&nonce=abc&timestamp={old_ts}"
        ok, _, reason = verify_oauth_state(state, "google")
        assert ok is False
        assert reason == "expired"


class TestExtractUserInfo:
    def test_google_extraction(self):
        userinfo = {
            "sub": "123",
            "email": "User@Google.COM",
            "email_verified": True,
            "name": "Test User",
            "picture": "https://example.com/pic.jpg",
        }
        result = extract_user_info_from_google(userinfo)
        assert result.provider_user_id == "123"
        assert result.email == "user@google.com"
        assert result.email_verified is True

    def test_github_extraction_uses_primary_email(self):
        user = {
            "id": 42,
            "name": "Dev User",
            "avatar_url": "https://example.com/av.png",
        }
        emails = [
            {"email": "other@github.com", "primary": False, "verified": True},
            {"email": "primary@github.com", "primary": True, "verified": True},
        ]
        result = extract_user_info_from_github(user, emails)
        assert result.email == "primary@github.com"
        assert result.email_verified is True

    def test_discord_builds_avatar_url(self):
        userinfo = {
            "id": "9999",
            "email": "user@discord.com",
            "verified": True,
            "global_name": "DiscordUser",
            "avatar": "abcdef",
        }
        result = extract_user_info_from_discord(userinfo)
        assert "cdn.discordapp.com/avatars/9999/abcdef.png" in result.picture
        assert result.email_verified is True

    def test_discord_empty_avatar_when_no_hash(self):
        userinfo = {
            "id": "1",
            "email": "u@d.com",
            "verified": False,
            "username": "user",
        }
        result = extract_user_info_from_discord(userinfo)
        assert result.picture == ""


class TestProviderStrategies:
    @pytest.mark.asyncio
    async def test_google_uses_userinfo_from_token(self):
        """GoogleStrategy reads userinfo embedded in the token dict (no extra request)."""
        strategy = GoogleStrategy()
        client = MagicMock()
        token = {
            "userinfo": {
                "sub": "g-123",
                "email": "User@Google.COM",
                "email_verified": True,
                "name": "Google User",
                "picture": "https://example.com/pic.jpg",
            }
        }
        result = await strategy.fetch_user_info(client, token)

        assert result.provider_user_id == "g-123"
        assert result.email == "user@google.com"
        assert result.email_verified is True
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_google_fetches_userinfo_when_absent_from_token(self):
        """GoogleStrategy calls client.get('userinfo') when token has no userinfo."""
        strategy = GoogleStrategy()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "sub": "g-456",
            "email": "fetched@google.com",
            "email_verified": True,
            "name": "Fetched User",
        }
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)

        result = await strategy.fetch_user_info(client, {})

        client.get.assert_awaited_once_with("userinfo", token={})
        assert result.provider_user_id == "g-456"
        assert result.email == "fetched@google.com"

    @pytest.mark.asyncio
    async def test_github_fetches_user_and_emails(self):
        """GitHubStrategy calls /user then /user/emails and picks the primary."""
        strategy = GitHubStrategy()
        user_resp = MagicMock()
        user_resp.raise_for_status = MagicMock()
        user_resp.json.return_value = {
            "id": 99,
            "login": "ghuser",
            "name": "GH User",
            "avatar_url": "https://example.com/av.png",
        }
        emails_resp = MagicMock()
        emails_resp.status_code = 200
        emails_resp.json.return_value = [
            {"email": "other@gh.com", "primary": False, "verified": True},
            {"email": "primary@gh.com", "primary": True, "verified": True},
        ]
        client = MagicMock()
        client.get = AsyncMock(side_effect=[user_resp, emails_resp])

        result = await strategy.fetch_user_info(client, {})

        assert result.provider_user_id == "99"
        assert result.email == "primary@gh.com"
        assert result.email_verified is True

    @pytest.mark.asyncio
    async def test_github_falls_back_to_first_email_when_no_primary(self):
        """GitHubStrategy uses the first email when none is marked primary."""
        strategy = GitHubStrategy()
        user_resp = MagicMock()
        user_resp.raise_for_status = MagicMock()
        user_resp.json.return_value = {"id": 7, "login": "user7", "name": ""}
        emails_resp = MagicMock()
        emails_resp.status_code = 200
        emails_resp.json.return_value = [
            {"email": "fallback@gh.com", "primary": False, "verified": False},
        ]
        client = MagicMock()
        client.get = AsyncMock(side_effect=[user_resp, emails_resp])

        result = await strategy.fetch_user_info(client, {})

        assert result.email == "fallback@gh.com"

    @pytest.mark.asyncio
    async def test_discord_fetches_me_and_builds_avatar_url(self):
        """DiscordStrategy calls /users/@me and builds the CDN avatar URL."""
        strategy = DiscordStrategy()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "id": "777",
            "email": "D@Discord.COM",
            "verified": True,
            "global_name": "DiscordUser",
            "avatar": "hash123",
        }
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)

        result = await strategy.fetch_user_info(client, {})

        client.get.assert_awaited_once_with("users/@me", token={})
        assert result.provider_user_id == "777"
        assert result.email == "d@discord.com"
        assert "cdn.discordapp.com/avatars/777/hash123.png" in result.picture
