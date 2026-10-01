"""
Account deletion endpoints — DELETE /api/v1/me and POST /auth/restore.

Route tests run a REAL AccountDeletionService over an AsyncMock user
repository (the re-auth and status-guard logic lives in the service, and
"the account stayed ACTIVE" is provable as "the guarded transition was
never called"). The login/OAuth status gates are covered at the service
layer next to the other credential/OAuth service tests; here the login
route pins the wire contract (403 + ``X-Error-Code``).

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import (
    CurrentUser,
    get_account_deletion_service,
    get_credential_service,
    require_jwt,
)
from app.errors import AccountPendingDeletionError, AuthenticationError
from app.infrastructure.crypto import hash_password
from app.routes.api_v1 import router as api_v1_router
from app.routes.auth import router as auth_router
from app.schemas.models.user import UserDoc
from app.services.account_deletion_service import AccountDeletionService
from tests.conftest import build_test_app

# ── Helpers ──────────────────────────────────────────────────────────────────

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
PASSWORD = "CorrectHorse1!"
GRACE_DAYS = 9
PURGE_AFTER = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def make_user_doc(
    email="test@example.com",
    email_verified=True,
    password_set=False,
    password_hash=None,
    status="ACTIVE",
    purge_after=None,
):
    return UserDoc.from_mongo(
        {
            "_id": USER_OID,
            "email": email,
            "email_verified": email_verified,
            "password_hash": password_hash,
            "password_set": password_set,
            "user_name": "Test User",
            "pfp": None,
            "auth_providers": [],
            "plan": "free",
            "signup_ip": None,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "status": status,
            "purge_after": purge_after,
        }
    )


class RecordingMailer:
    """ErasureMailer stand-in recording notices and cancellations."""

    def __init__(self):
        self.requested: list[tuple[str, datetime, str | None]] = []
        self.cancelled: list[str] = []

    async def send_deletion_requested(self, email, purge_after, restore_token=None):
        self.requested.append((email, purge_after, restore_token))

    async def send_deletion_cancelled(self, email):
        self.cancelled.append(email)

    async def send_erasure_confirmation(self, email):
        return None


class FakeTokenRepo:
    """In-memory verification-tokens repo — real hash matching so the
    one-shot restore token's single-use/expiry semantics are exercised."""

    def __init__(self):
        self.docs: list[dict] = []

    async def delete_by_user(self, user_id, token_type=None, app_id=None):
        before = len(self.docs)
        self.docs = [
            d
            for d in self.docs
            if not (
                d["user_id"] == user_id
                and (token_type is None or d["token_type"] == token_type)
            )
        ]
        return before - len(self.docs)

    async def create(self, data):
        self.docs.append(dict(data))
        return ObjectId()

    def _find_live(self, token_hash, token_type):
        now = datetime.now(timezone.utc)
        for d in self.docs:
            if (
                d["token_hash"] == token_hash
                and d["token_type"] == token_type
                and d["used_at"] is None
                and d["expires_at"] > now
            ):
                return d
        return None

    async def find_valid_by_hash(self, token_hash, token_type):
        from types import SimpleNamespace

        d = self._find_live(token_hash, token_type)
        return SimpleNamespace(**d) if d else None

    async def consume_by_hash(self, token_hash, token_type):
        from types import SimpleNamespace

        d = self._find_live(token_hash, token_type)
        if d is None:
            return None
        d["used_at"] = datetime.now(timezone.utc)
        return SimpleNamespace(**d)

    async def delete_by_hash(self, token_hash, token_type):
        before = len(self.docs)
        self.docs = [
            d
            for d in self.docs
            if not (d["token_hash"] == token_hash and d["token_type"] == token_type)
        ]
        return before - len(self.docs)


class FailingTokenRepo(FakeTokenRepo):
    """Persistence outage: every token write fails."""

    async def create(self, data):
        raise RuntimeError("mongo down")


def make_service(user, *, mailer=None, mark_result=True, token_repo=None):
    """Real AccountDeletionService over a mocked user repository.

    ``find_by_id`` answers *user* first (the re-auth fetch), then the
    post-flip read-back with ``purge_after`` stamped.
    """
    repo = AsyncMock()
    pending = make_user_doc(
        email=user.email,
        password_set=user.password_set,
        password_hash=user.password_hash,
        status="PENDING_DELETION",
        purge_after=PURGE_AFTER,
    )
    repo.find_by_id.side_effect = [user, pending]
    repo.find_by_email.return_value = user
    repo.mark_pending_deletion.return_value = mark_result
    repo.restore.return_value = True
    svc = AccountDeletionService(
        repo,
        token_repo=token_repo if token_repo is not None else FakeTokenRepo(),
        mailer=mailer,
        grace_days=GRACE_DAYS,
    )
    return svc, repo


def _make_jwt_user() -> CurrentUser:
    return CurrentUser(user_id=USER_OID, email_verified=True, api_key_doc=None)


def _client(svc: AccountDeletionService) -> TestClient:
    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt: _make_jwt_user,
            get_account_deletion_service: lambda: svc,
        },
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth_client(svc: AccountDeletionService) -> TestClient:
    app = build_test_app(
        auth_router, overrides={get_account_deletion_service: lambda: svc}
    )
    return TestClient(app, raise_server_exceptions=False)


# ── DELETE /api/v1/me — password accounts ────────────────────────────────────


def test_delete_me_wrong_password_403_and_account_stays_active():
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    svc, repo = make_service(user)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"password": "WrongPass1!"}
    )

    assert resp.status_code == 403
    # The guarded transition never ran — the account is still ACTIVE.
    repo.mark_pending_deletion.assert_not_awaited()


def test_delete_me_missing_reauth_403():
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    svc, repo = make_service(user)

    resp = _client(svc).request("DELETE", "/api/v1/me", json={})

    assert resp.status_code == 403
    repo.mark_pending_deletion.assert_not_awaited()


def test_delete_me_correct_password_200_with_purge_after():
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    mailer = RecordingMailer()
    svc, repo = make_service(user, mailer=mailer)

    resp = _client(svc).request("DELETE", "/api/v1/me", json={"password": PASSWORD})

    assert resp.status_code == 200
    assert resp.json() == {"purge_after": PURGE_AFTER.isoformat()}
    # The flip used the configured grace period.
    assert repo.mark_pending_deletion.await_args.args == (USER_OID, GRACE_DAYS)
    # The grace-period notice went out with the stored deadline and the
    # one-shot cancel link's token.
    (sent_email, sent_deadline, sent_token) = mailer.requested[0]
    assert (sent_email, sent_deadline) == (user.email, PURGE_AFTER)
    assert sent_token is not None


def test_delete_me_confirm_email_rejected_for_password_account():
    """A hijacked session must not bypass the password with the typed email."""
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    svc, repo = make_service(user)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": user.email}
    )

    assert resp.status_code == 403
    repo.mark_pending_deletion.assert_not_awaited()


# ── DELETE /api/v1/me — OAuth-only accounts ──────────────────────────────────


def test_delete_me_oauth_only_confirm_email_succeeds():
    user = make_user_doc(password_set=False, password_hash=None)
    mailer = RecordingMailer()
    tokens = FakeTokenRepo()
    svc, _repo = make_service(user, mailer=mailer, token_repo=tokens)

    before = datetime.now(timezone.utc)
    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )
    after = datetime.now(timezone.utc)

    assert resp.status_code == 200
    # The OAuth-only path computes the deadline itself (now + grace) and
    # threads the same instant into the flip and the token expiry.
    purge_after = datetime.fromisoformat(resp.json()["purge_after"])
    assert before + timedelta(days=GRACE_DAYS) <= purge_after
    assert purge_after <= after + timedelta(days=GRACE_DAYS)
    # The persisted restore proof expires exactly at the stored deadline
    # and the notice carried its one-shot link.
    assert [d["expires_at"] for d in tokens.docs] == [purge_after]
    (_email, sent_deadline, sent_token) = mailer.requested[0]
    assert sent_deadline == purge_after
    assert sent_token is not None


def test_delete_me_oauth_only_token_persisted_before_flip():
    """The invariant: an OAuth-only account is never PENDING_DELETION
    without a persisted restore token — so the mint must land first."""
    user = make_user_doc(password_set=False, password_hash=None)
    tokens = FakeTokenRepo()
    svc, repo = make_service(user, token_repo=tokens)

    tokens_at_flip = []

    async def record_flip(*args, **kwargs):
        tokens_at_flip.append(len(tokens.docs))
        return True

    repo.mark_pending_deletion.side_effect = record_flip

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )

    assert resp.status_code == 200
    assert tokens_at_flip == [1]


def test_delete_me_oauth_only_token_failure_500_account_stays_active():
    """Minting the restore proof failed: the request must fail (500)
    BEFORE the flip — a failed request is recoverable, a proof-less
    pending deletion is not (no credential restore, re-request 409s)."""
    user = make_user_doc(password_set=False, password_hash=None)
    mailer = RecordingMailer()
    tokens = FailingTokenRepo()
    svc, repo = make_service(user, mailer=mailer, token_repo=tokens)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )

    assert resp.status_code == 500
    # The guarded transition never ran — the account is still ACTIVE.
    repo.mark_pending_deletion.assert_not_awaited()
    # No token left behind, no notice mailed.
    assert tokens.docs == []
    assert mailer.requested == []


def test_delete_me_oauth_only_flip_race_cleans_up_token():
    """Lost the flip race AFTER minting: 409, and the just-minted (never
    emailed) token is deleted so it can't linger as a stray proof."""
    user = make_user_doc(password_set=False, password_hash=None)
    mailer = RecordingMailer()
    tokens = FakeTokenRepo()
    svc, _repo = make_service(user, mailer=mailer, mark_result=False, token_repo=tokens)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )

    assert resp.status_code == 409
    assert tokens.docs == []
    assert mailer.requested == []


def test_delete_me_oauth_only_flip_exception_cleans_up_token():
    """A flip that ERRORS (not just loses the race) also cleans up the
    minted token before propagating."""
    user = make_user_doc(password_set=False, password_hash=None)
    tokens = FakeTokenRepo()
    svc, repo = make_service(user, token_repo=tokens)
    repo.mark_pending_deletion.side_effect = RuntimeError("mongo down")

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )

    assert resp.status_code == 500
    assert tokens.docs == []


def test_delete_me_password_token_failure_still_pending_mail_without_link():
    """Password accounts keep best-effort minting AFTER the flip — they
    always have credential restore, so a lost link must not fail the
    deletion request; the notice just omits the link."""
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    mailer = RecordingMailer()
    svc, repo = make_service(user, mailer=mailer, token_repo=FailingTokenRepo())

    resp = _client(svc).request("DELETE", "/api/v1/me", json={"password": PASSWORD})

    assert resp.status_code == 200
    repo.mark_pending_deletion.assert_awaited_once()
    (_email, _deadline, sent_token) = mailer.requested[0]
    assert sent_token is None


def test_delete_me_oauth_only_wrong_confirm_email_403():
    user = make_user_doc(password_set=False, password_hash=None)
    svc, repo = make_service(user)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "other@example.com"}
    )

    assert resp.status_code == 403
    repo.mark_pending_deletion.assert_not_awaited()


def test_delete_me_oauth_only_password_path_rejected():
    """No password exists — supplying one must never re-authenticate."""
    user = make_user_doc(password_set=False, password_hash=None)
    svc, repo = make_service(user)

    resp = _client(svc).request("DELETE", "/api/v1/me", json={"password": "AnyPass1!"})

    assert resp.status_code == 403
    repo.mark_pending_deletion.assert_not_awaited()


# ── DELETE /api/v1/me — repeat + auth ────────────────────────────────────────


def test_delete_me_second_request_409():
    user = make_user_doc(
        password_set=True,
        password_hash=hash_password(PASSWORD),
        status="PENDING_DELETION",
        purge_after=PURGE_AFTER,
    )
    svc, repo = make_service(user)

    resp = _client(svc).request("DELETE", "/api/v1/me", json={"password": PASSWORD})

    assert resp.status_code == 409
    repo.mark_pending_deletion.assert_not_awaited()


def test_delete_me_lost_race_409():
    """Guarded transition returns False (concurrent request won) → 409."""
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    svc, _repo = make_service(user, mark_result=False)

    resp = _client(svc).request("DELETE", "/api/v1/me", json={"password": PASSWORD})

    assert resp.status_code == 409


def test_delete_me_requires_auth():
    def _raise_unauth() -> CurrentUser:
        raise AuthenticationError("Authentication required")

    app = build_test_app(
        api_v1_router,
        overrides={
            require_jwt: _raise_unauth,
            get_account_deletion_service: lambda: AsyncMock(),
        },
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.request("DELETE", "/api/v1/me", json={"password": PASSWORD})

    assert resp.status_code == 401


# ── Login while pending — wire contract ──────────────────────────────────────


def test_login_while_pending_403_with_error_code_header():
    credential_svc = AsyncMock()
    credential_svc.login.side_effect = AccountPendingDeletionError(
        "this account is scheduled for deletion"
    )
    app = build_test_app(
        auth_router, overrides={get_credential_service: lambda: credential_svc}
    )

    # Context-managed: the login route reads jwt config off app.state,
    # which only exists once the (mock) lifespan has run.
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/auth/login", json={"email": "test@example.com", "password": PASSWORD}
        )

    assert resp.status_code == 403
    assert resp.headers["X-Error-Code"] == "ACCOUNT_PENDING_DELETION"
    assert resp.json()["code"] == "ACCOUNT_PENDING_DELETION"


# ── POST /auth/restore ───────────────────────────────────────────────────────


def _pending_user(password_hash):
    return make_user_doc(
        password_set=True,
        password_hash=password_hash,
        status="PENDING_DELETION",
        purge_after=PURGE_AFTER,
    )


def test_restore_happy_path_reactivates_account():
    hashed = hash_password(PASSWORD)
    mailer = RecordingMailer()
    svc, repo = make_service(_pending_user(hashed), mailer=mailer)
    repo.find_by_email.return_value = _pending_user(hashed)
    client = _auth_client(svc)

    resp = client.post(
        "/auth/restore", json={"email": "test@example.com", "password": PASSWORD}
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert repo.restore.await_args.args == (USER_OID,)
    # The account address hears about the cancellation — a silent restore
    # would hide an attacker cancelling a victim's deletion.
    assert mailer.cancelled == ["test@example.com"]


def test_restore_failures_are_uniform_403():
    """Wrong password, not-pending, unknown email, and purged all answer
    with an identical 403 body — no account enumeration."""
    hashed = hash_password(PASSWORD)
    bodies = []

    # Wrong password.
    svc, repo = make_service(_pending_user(hashed))
    repo.find_by_email.return_value = _pending_user(hashed)
    resp = _auth_client(svc).post(
        "/auth/restore", json={"email": "test@example.com", "password": "Wrong1!"}
    )
    assert resp.status_code == 403
    repo.restore.assert_not_awaited()
    bodies.append(resp.json())

    # Not pending (repo guard refuses the transition).
    svc, repo = make_service(_pending_user(hashed))
    repo.find_by_email.return_value = make_user_doc(
        password_set=True, password_hash=hashed, status="ACTIVE"
    )
    repo.restore.return_value = False
    resp = _auth_client(svc).post(
        "/auth/restore", json={"email": "test@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 403
    bodies.append(resp.json())

    # Unknown email — including an account already purged by the sweep.
    svc, repo = make_service(_pending_user(hashed))
    repo.find_by_email.return_value = None
    resp = _auth_client(svc).post(
        "/auth/restore", json={"email": "gone@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 403
    bodies.append(resp.json())

    assert bodies[0] == bodies[1] == bodies[2]


def test_restore_after_purge_403():
    """User doc already erased by the sweep — same uniform 403."""
    svc, repo = make_service(_pending_user(hash_password(PASSWORD)))
    repo.find_by_email.return_value = None
    client = _auth_client(svc)

    resp = client.post(
        "/auth/restore", json={"email": "test@example.com", "password": PASSWORD}
    )

    assert resp.status_code == 403
    repo.restore.assert_not_awaited()


def test_restore_oauth_only_account_403():
    """No password hash — the endpoint can't validate, uniform 403."""
    svc, repo = make_service(_pending_user(None))
    repo.find_by_email.return_value = make_user_doc(
        password_set=False, password_hash=None, status="PENDING_DELETION"
    )
    client = _auth_client(svc)

    resp = client.post(
        "/auth/restore", json={"email": "test@example.com", "password": PASSWORD}
    )

    assert resp.status_code == 403
    repo.restore.assert_not_awaited()


# ── POST /auth/restore — one-shot token path (OAuth-only accounts) ───────────


def _oauth_pending_service(mailer=None, token_repo=None):
    """OAuth-only account already PENDING_DELETION, with a shared fake
    token repo so minted tokens are consumable across requests."""
    token_repo = token_repo or FakeTokenRepo()
    user = make_user_doc(password_set=False, password_hash=None)
    svc, repo = make_service(user, mailer=mailer, token_repo=token_repo)
    return svc, repo, token_repo


def test_restore_with_token_full_loop_for_oauth_only_account():
    """The loop a stolen session could previously make irreversible:
    request deletion via confirm_email, cancel via the emailed link."""
    mailer = RecordingMailer()
    svc, repo, _tokens = _oauth_pending_service(mailer=mailer)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )
    assert resp.status_code == 200
    (_email, _deadline, token) = mailer.requested[0]
    assert token is not None

    resp = _auth_client(svc).post("/auth/restore", json={"restore_token": token})

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert repo.restore.await_args.args == (USER_OID,)
    assert mailer.cancelled == ["test@example.com"]


def test_restore_token_is_single_use():
    mailer = RecordingMailer()
    svc, repo, _tokens = _oauth_pending_service(mailer=mailer)
    _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )
    token = mailer.requested[0][2]
    client = _auth_client(svc)

    assert (
        client.post("/auth/restore", json={"restore_token": token}).status_code == 200
    )
    resp = client.post("/auth/restore", json={"restore_token": token})

    assert resp.status_code == 403
    assert repo.restore.await_count == 1


def test_restore_token_expired_403():
    mailer = RecordingMailer()
    svc, repo, tokens = _oauth_pending_service(mailer=mailer)
    _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )
    token = mailer.requested[0][2]
    # The grace period ended — expiry rides purge_after.
    tokens.docs[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    resp = _auth_client(svc).post("/auth/restore", json={"restore_token": token})

    assert resp.status_code == 403
    repo.restore.assert_not_awaited()


def test_restore_token_for_erasing_account_403():
    """The cascade already claimed the account — the guarded restore
    refuses (uniform 403) and the token stays unconsumed but useless:
    ERASING is terminal, and the cascade deletes the token doc anyway."""
    mailer = RecordingMailer()
    svc, repo, tokens = _oauth_pending_service(mailer=mailer)
    _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )
    token = mailer.requested[0][2]
    repo.restore.return_value = False

    resp = _auth_client(svc).post("/auth/restore", json={"restore_token": token})

    assert resp.status_code == 403
    assert mailer.cancelled == []
    assert tokens.docs[0]["used_at"] is None


def test_restore_token_survives_transient_flip_failure():
    """A transient error during the guarded flip must not burn the
    single-use token — it is the ONLY cancel proof an OAuth-only account
    has. Validate without consuming, flip, consume only on success."""
    mailer = RecordingMailer()
    svc, repo, tokens = _oauth_pending_service(mailer=mailer)
    _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": "test@example.com"}
    )
    token = mailer.requested[0][2]
    repo.restore.side_effect = [RuntimeError("mongo down"), True]
    client = _auth_client(svc)

    resp = client.post("/auth/restore", json={"restore_token": token})

    assert resp.status_code == 500
    # The token survived the outage — the retry restores the account.
    assert tokens.docs[0]["used_at"] is None
    retry = client.post("/auth/restore", json={"restore_token": token})
    assert retry.status_code == 200
    assert tokens.docs[0]["used_at"] is not None
    assert mailer.cancelled == ["test@example.com"]


@pytest.mark.asyncio
async def test_restore_token_double_restore_race_single_winner():
    """Two racers both validate the token before either consumes it; the
    guarded PENDING_DELETION-only flip picks one winner, the loser gets
    the uniform 403, and only the winner consumes the token."""
    from types import SimpleNamespace

    from app.errors import ForbiddenError

    token_repo = AsyncMock()
    token_repo.find_valid_by_hash.return_value = SimpleNamespace(
        user_id=USER_OID, email="test@example.com"
    )
    repo = AsyncMock()
    repo.restore.side_effect = [True, False]
    svc = AccountDeletionService(repo, token_repo=token_repo, grace_days=GRACE_DAYS)

    await svc.restore_with_token("x" * 43)
    with pytest.raises(ForbiddenError):
        await svc.restore_with_token("x" * 43)

    assert repo.restore.await_count == 2
    token_repo.consume_by_hash.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_token_consume_failure_after_restore_is_harmless():
    """Consuming is best-effort AFTER a successful flip: replay of the
    unburned token hits the PENDING_DELETION-only filter and 403s, so a
    failed consume must not fail the restore the user already won."""
    from types import SimpleNamespace

    token_repo = AsyncMock()
    token_repo.find_valid_by_hash.return_value = SimpleNamespace(
        user_id=USER_OID, email="test@example.com"
    )
    token_repo.consume_by_hash.side_effect = RuntimeError("mongo down")
    repo = AsyncMock()
    repo.restore.return_value = True
    mailer = RecordingMailer()
    svc = AccountDeletionService(
        repo, token_repo=token_repo, mailer=mailer, grace_days=GRACE_DAYS
    )

    await svc.restore_with_token("x" * 43)

    assert mailer.cancelled == ["test@example.com"]


def test_restore_unknown_token_403():
    svc, repo, _tokens = _oauth_pending_service()

    resp = _auth_client(svc).post("/auth/restore", json={"restore_token": "x" * 43})

    assert resp.status_code == 403
    repo.restore.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": "test@example.com"},
        {"password": PASSWORD},
        {
            "email": "test@example.com",
            "password": PASSWORD,
            "restore_token": "x" * 43,
        },
        {"email": "test@example.com", "restore_token": "x" * 43},
    ],
    ids=["empty", "email_only", "password_only", "both_proofs", "email_plus_token"],
)
def test_restore_wrong_shape_rejected_422(body):
    """Mixed or incomplete proofs are a validation error — they never
    reach the service, so they can't probe account state."""
    svc, repo = make_service(_pending_user(hash_password(PASSWORD)))

    resp = _auth_client(svc).post("/auth/restore", json=body)

    assert resp.status_code == 422
    repo.restore.assert_not_awaited()


def test_restore_token_deleted_after_credential_restore():
    """A credential restore invalidates the emailed link — the one-shot
    token must not survive the deletion it was minted to cancel."""
    hashed = hash_password(PASSWORD)
    mailer = RecordingMailer()
    user = make_user_doc(
        password_set=True,
        password_hash=hashed,
        status="PENDING_DELETION",
        purge_after=PURGE_AFTER,
    )
    tokens = FakeTokenRepo()
    tokens.docs.append(
        {
            "user_id": USER_OID,
            "email": user.email,
            "token_hash": "h" * 64,
            "token_type": "deletion_restore",
            "expires_at": PURGE_AFTER,
            "used_at": None,
        }
    )
    svc, repo = make_service(user, mailer=mailer, token_repo=tokens)
    repo.find_by_email.return_value = user

    resp = _auth_client(svc).post(
        "/auth/restore", json={"email": user.email, "password": PASSWORD}
    )

    assert resp.status_code == 200
    assert tokens.docs == []


# ── Re-auth fail-closed — password_set without a hash ────────────────────────


def test_delete_me_password_set_without_hash_fails_closed():
    """An inconsistent doc (flag set, hash missing) must 403 — falling
    through to typed-email would let a hijacked session skip the password."""
    user = make_user_doc(password_set=True, password_hash=None)
    svc, repo = make_service(user)

    resp = _client(svc).request(
        "DELETE", "/api/v1/me", json={"confirm_email": user.email}
    )

    assert resp.status_code == 403
    repo.mark_pending_deletion.assert_not_awaited()


# ── Timing equalization (CWE-203) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_unknown_email_burns_dummy_verify(monkeypatch):
    """Early-failure paths run a verify against the module's dummy hash so
    timing can't distinguish "no such account" from "wrong password"."""
    import app.services.account_deletion_service as ads

    verified_hashes = []
    monkeypatch.setattr(
        ads,
        "verify_password",
        lambda pwd, hsh: verified_hashes.append(hsh) or False,
    )
    svc, repo = make_service(_pending_user(hash_password(PASSWORD)))
    repo.find_by_email.return_value = None

    from app.errors import ForbiddenError

    with pytest.raises(ForbiddenError):
        await svc.restore("gone@example.com", PASSWORD)

    assert verified_hashes == [ads._TIMING_DUMMY_HASH]


@pytest.mark.asyncio
async def test_restore_oauth_only_burns_dummy_verify(monkeypatch):
    import app.services.account_deletion_service as ads

    verified_hashes = []
    monkeypatch.setattr(
        ads,
        "verify_password",
        lambda pwd, hsh: verified_hashes.append(hsh) or False,
    )
    svc, repo = make_service(_pending_user(None))
    repo.find_by_email.return_value = make_user_doc(
        password_set=False, password_hash=None, status="PENDING_DELETION"
    )

    from app.errors import ForbiddenError

    with pytest.raises(ForbiddenError):
        await svc.restore("test@example.com", PASSWORD)

    assert verified_hashes == [ads._TIMING_DUMMY_HASH]


# ── Service seam — mail failure never blocks the request ────────────────────


class ExplodingMailer(RecordingMailer):
    async def send_deletion_requested(self, email, purge_after, restore_token=None):
        raise RuntimeError("smtp down")

    async def send_deletion_cancelled(self, email):
        raise RuntimeError("smtp down")


@pytest.mark.asyncio
async def test_request_deletion_survives_mail_outage():
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    svc, _repo = make_service(user, mailer=ExplodingMailer())

    purge_after = await svc.request_deletion(
        USER_OID, password=PASSWORD, confirm_email=None
    )

    assert purge_after == PURGE_AFTER


@pytest.mark.asyncio
async def test_request_deletion_computes_deadline_when_readback_races():
    """Read-back losing a race with the sweep still yields a deadline."""
    user = make_user_doc(password_set=True, password_hash=hash_password(PASSWORD))
    repo = AsyncMock()
    repo.find_by_id.side_effect = [user, None]
    repo.mark_pending_deletion.return_value = True
    svc = AccountDeletionService(repo, grace_days=GRACE_DAYS)

    before = datetime.now(timezone.utc)
    purge_after = await svc.request_deletion(
        USER_OID, password=PASSWORD, confirm_email=None
    )

    assert purge_after >= before + timedelta(days=GRACE_DAYS, seconds=-5)


def test_get_account_deletion_service_reads_app_state():
    """The real dependency (overridden everywhere above) resolves from
    app.state, where wire_services parks the singleton."""
    from unittest.mock import MagicMock

    request = MagicMock()
    resolved = get_account_deletion_service(request)
    assert resolved is request.app.state.account_deletion_service
