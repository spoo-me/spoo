"""Post-callback redirect resolution — routes/oauth_routes.py."""

from __future__ import annotations

from app.routes.oauth_routes import resolve_post_auth_redirect


def test_new_account_goes_to_onboarding_when_enabled():
    assert (
        resolve_post_auth_redirect("", is_new=True, onboarding_enabled=True)
        == "/onboarding"
    )


def test_flag_off_keeps_new_accounts_on_default():
    # Nothing serves /onboarding until the frontend cutover — a new account
    # must land on the default redirect, not a 404.
    assert (
        resolve_post_auth_redirect("", is_new=True, onboarding_enabled=False)
        == "/dashboard"
    )


def test_explicit_next_wins_over_onboarding():
    # Device-consent deep links carry an explicit destination that must
    # survive even for brand-new accounts.
    assert (
        resolve_post_auth_redirect(
            "/auth/device/callback?code=x", is_new=True, onboarding_enabled=True
        )
        == "/auth/device/callback?code=x"
    )


def test_existing_account_never_redirects_to_onboarding():
    assert (
        resolve_post_auth_redirect("", is_new=False, onboarding_enabled=True)
        == "/dashboard"
    )


def test_unsafe_next_still_falls_back():
    assert (
        resolve_post_auth_redirect("//evil.com", is_new=True, onboarding_enabled=True)
        == "/dashboard"
    )
