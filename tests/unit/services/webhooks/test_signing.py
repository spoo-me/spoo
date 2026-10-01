"""Standard Webhooks signing — shape, roundtrip, rotation grace."""

from __future__ import annotations

import base64

from app.services.webhooks.signing import (
    SECRET_PREFIX,
    generate_signing_secret,
    new_webhook_id,
    secret_display_prefix,
    sign,
    verify,
)


class TestSecrets:
    def test_secret_shape(self):
        secret = generate_signing_secret()
        assert secret.startswith(SECRET_PREFIX)
        # base64(24 bytes) after the prefix
        assert len(base64.b64decode(secret.removeprefix(SECRET_PREFIX))) == 24

    def test_secrets_unique(self):
        assert generate_signing_secret() != generate_signing_secret()

    def test_display_prefix(self):
        secret = generate_signing_secret()
        prefix = secret_display_prefix(secret)
        assert secret.startswith(prefix)
        assert len(prefix) == len(SECRET_PREFIX) + 8


class TestSigning:
    def test_known_vector(self):
        # Fixed vector: pin the exact Standard Webhooks construction —
        # v1,base64(HMAC-SHA256(key, "{id}.{ts}.{body}")) with the key being
        # the base64-DECODED portion after whsec_.
        secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
        signature = sign(
            "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, '{"test": 2432232314}', secret
        )
        assert signature == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

    def test_verify_roundtrip(self):
        secret = generate_signing_secret()
        sig = sign("msg_1", 1700000000, '{"a":1}', secret)
        assert verify("msg_1", 1700000000, '{"a":1}', secret, sig)

    def test_verify_rejects_tampered_body(self):
        secret = generate_signing_secret()
        sig = sign("msg_1", 1700000000, '{"a":1}', secret)
        assert not verify("msg_1", 1700000000, '{"a":2}', secret, sig)

    def test_verify_accepts_any_of_space_delimited_signatures(self):
        """Rotation grace: old + new signatures ride one header."""
        old, new = generate_signing_secret(), generate_signing_secret()
        header = " ".join(
            [sign("msg_1", 1700000000, "{}", new), sign("msg_1", 1700000000, "{}", old)]
        )
        assert verify("msg_1", 1700000000, "{}", old, header)
        assert verify("msg_1", 1700000000, "{}", new, header)


class TestIds:
    def test_webhook_id_shape_and_uniqueness(self):
        a, b = new_webhook_id(), new_webhook_id()
        assert a.startswith("msg_") and b.startswith("msg_")
        assert a != b
