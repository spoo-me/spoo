"""Zero-config JWT bootstrap (_ensure_jwt_secret).

pyjwt refuses an empty HMAC key, so a deploy with no JWT config at all
would 500 on every register/login. The bootstrap autogenerates an
ephemeral secret instead — auth works out of the box, sessions just
don't survive restarts.
"""

from __future__ import annotations

import jwt as pyjwt

from app import _ensure_jwt_secret
from config import AppSettings, JWTSettings


def _settings(**jwt_kwargs) -> AppSettings:
    defaults = {"jwt_private_key": "", "jwt_public_key": "", "jwt_secret": ""}
    defaults.update(jwt_kwargs)
    return AppSettings(jwt=JWTSettings(**defaults))


class TestEnsureJwtSecret:
    def test_empty_config_autogenerates_usable_secret(self):
        settings = _settings()
        _ensure_jwt_secret(settings)

        secret = settings.jwt.jwt_secret
        assert len(secret) == 64  # token_hex(32)
        # The generated secret must actually sign — this is the exact call
        # that raised InvalidKeyError("HMAC key must not be empty") before.
        token = pyjwt.encode({"sub": "u1"}, secret, algorithm="HS256")
        assert pyjwt.decode(token, secret, algorithms=["HS256"])["sub"] == "u1"

    def test_configured_secret_is_untouched(self):
        secret = "s" * 40
        settings = _settings(jwt_secret=secret)
        _ensure_jwt_secret(settings)
        assert settings.jwt.jwt_secret == secret

    def test_short_secret_kept_but_not_replaced(self):
        settings = _settings(jwt_secret="short")
        _ensure_jwt_secret(settings)
        assert settings.jwt.jwt_secret == "short"

    def test_rs256_config_is_untouched(self):
        settings = _settings(jwt_private_key="fake-priv", jwt_public_key="fake-pub")
        _ensure_jwt_secret(settings)
        assert settings.jwt.jwt_secret == ""
        assert settings.jwt.use_rs256

    def test_generated_secrets_are_unique(self):
        a, b = _settings(), _settings()
        _ensure_jwt_secret(a)
        _ensure_jwt_secret(b)
        assert a.jwt.jwt_secret != b.jwt.jwt_secret
