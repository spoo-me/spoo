"""End-to-end erasure cascade against real MongoDB.

The mock suite asserts filter shapes; it cannot see one query undo another's
work. The regression that proved that: the domain cascade hard-deleting the
BLOCKED links the owner-scoped delete had just retained and scrubbed. These
tests run the full ``erase()`` over real documents and read back what
actually survived.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from repositories.user_repository import UserRepository
from schemas.models.base import ANONYMOUS_OWNER_ID

# Naive UTC, no microseconds: Mongo stores millisecond-truncated naive UTC,
# so inserted values read back equal and equality asserts stay exact.
CREATED_AT = datetime(2026, 7, 1, 9, 0, 0)
BLOCKED_AT = datetime(2026, 8, 1, 12, 0, 0)
BLOCKED_REASON = "phishing: credential harvesting page"

A_EMAIL = "erased-user@example.com"
B_EMAIL = "bystander@example.com"


# ── Seed helpers ─────────────────────────────────────────────────────────────


async def insert_user(db, email: str) -> ObjectId:
    result = await db["users"].insert_one(
        {
            "email": email,
            "email_verified": True,
            "user_name": email.split("@")[0],
            "status": "ACTIVE",
            "signup_ip": "203.0.113.7",
            "created_at": CREATED_AT,
        }
    )
    return result.inserted_id


async def insert_link(
    db,
    owner_id: ObjectId,
    alias: str,
    domain: str,
    *,
    blocked: bool = False,
    with_meta_tags: bool = False,
    claimed: bool = False,
) -> ObjectId:
    doc: dict = {
        "alias": alias,
        "owner_id": owner_id,
        "domain": domain,
        "created_at": CREATED_AT,
        "creation_ip": "198.51.100.42",
        "long_url": f"https://destination.example/{alias}",
        "password": "$argon2id$v=19$m=65536,t=3,p=4$fakefakefake",
        "status": "BLOCKED" if blocked else "ACTIVE",
        "private_stats": True,
        "total_clicks": 1,
    }
    if blocked:
        doc["blocked_at"] = BLOCKED_AT
        doc["blocked_reason"] = BLOCKED_REASON
    if with_meta_tags:
        doc["meta_tags"] = {
            "title": "Preview title",
            "updated_at": CREATED_AT,
            "updated_ip": "198.51.100.42",
        }
    if claimed:
        doc["claimed_at"] = CREATED_AT
    result = await db["urlsV2"].insert_one(doc)
    return result.inserted_id


async def insert_click(
    db, url_id: ObjectId, owner_id: ObjectId, short_code: str, domain: str
) -> None:
    await db["clicks"].insert_one(
        {
            "clicked_at": CREATED_AT,
            "meta": {
                "url_id": url_id,
                "short_code": short_code,
                "owner_id": owner_id,
                "domain": domain,
            },
            "ip_address": "192.0.2.11",
            "country": "Germany",
            "city": "Berlin",
            "browser": "Firefox",
            "os": "Linux",
            "redirect_ms": 7,
            "device": "desktop",
        }
    )


async def insert_custom_domain(db, owner_id: ObjectId, fqdn: str) -> ObjectId:
    result = await db["custom_domains"].insert_one(
        {
            "fqdn": fqdn,
            "owner_id": owner_id,
            "status": "active",
            "verification_method": "cname",
            "created_at": CREATED_AT,
            "last_verified_at": CREATED_AT,
        }
    )
    return result.inserted_id


async def insert_page_layout(db, user_id: ObjectId) -> None:
    await db["page-layouts"].insert_one(
        {
            "user_id": user_id,
            "page": "overview",
            "layout": {"widgets": [{"id": "clicks", "x": 0, "y": 0}]},
            "updated_at": CREATED_AT,
        }
    )


async def insert_verification_token(db, user_id: ObjectId, email: str) -> None:
    await db["verification-tokens"].insert_one(
        {
            "user_id": user_id,
            "email": email,
            "token_hash": f"hash-{user_id}",
            "token_type": "email_verify",
            "expires_at": datetime(2027, 1, 1, 0, 0, 0),
            "created_at": CREATED_AT,
            "attempts": 0,
        }
    )


async def make_purge_due(db, user_id: ObjectId) -> None:
    """Flip the account purge-due through the real guarded transition."""
    flipped = await UserRepository(db["users"]).mark_pending_deletion(
        user_id, grace_days=0, now=datetime.now(timezone.utc)
    )
    assert flipped


# ── Test A: blocked-link retention through the whole cascade ────────────────


async def test_erase_retains_blocked_links_through_domain_cascade(
    real_db, erasure_service
):
    db = real_db
    user_a = await insert_user(db, A_EMAIL)
    fqdn = "links.erased-user.example"
    await insert_custom_domain(db, user_a, fqdn)

    active_default = await insert_link(db, user_a, "aact1", "spoo.me")
    blocked_default = await insert_link(
        db, user_a, "ablk1", "spoo.me", blocked=True, with_meta_tags=True
    )
    active_custom = await insert_link(db, user_a, "aact2", fqdn)
    blocked_custom = await insert_link(db, user_a, "ablk2", fqdn, blocked=True)
    all_link_ids = [active_default, blocked_default, active_custom, blocked_custom]

    for url_id, alias, domain in (
        (active_default, "aact1", "spoo.me"),
        (blocked_default, "ablk1", "spoo.me"),
        (active_custom, "aact2", fqdn),
        (blocked_custom, "ablk2", fqdn),
    ):
        await insert_click(db, url_id, user_a, alias, domain)
    # Pre-claim click: anonymous sentinel owner, tied only by url_id.
    await insert_click(db, blocked_default, ANONYMOUS_OWNER_ID, "ablk1", "spoo.me")
    # Mop-up path: owner-stamped click on a link deleted before erasure.
    await insert_click(db, ObjectId(), user_a, "gone123", "spoo.me")

    # One tag so the cascade's tags step is exercised, not just counted.
    await db["tags"].insert_one(
        {
            "owner_id": user_a,
            "name": "launch",
            "color": "violet",
            "icon": "tag",
            "created_at": CREATED_AT,
        }
    )

    await make_purge_due(db, user_a)
    counts = await erasure_service.erase(user_a)

    assert counts == {
        "urlsV2": 2,
        "urls_blocked_retained": 2,
        "custom_domains": 1,
        "tags": 1,
        "clicks": 6,
        "api_keys": 0,
        "verification_tokens": 0,
        "page_layouts": 0,
        "app_grants": 0,
        "webhook_endpoints": 0,
        "webhook_events": 0,
        "webhook_deliveries": 0,
        "report_submissions": 0,
        "reports_pulled": 0,
        "feature_flags_pulled": 0,
        "subscriptions": 0,
        "entitlement_overrides": 0,
        "entitlement_events": 0,
        "r2_objects": 0,
    }

    # BLOCKED docs survive on BOTH domains — the custom-domain one is the
    # regression: the domain cascade must not undo the owner-scoped retention.
    for blocked_id in (blocked_default, blocked_custom):
        doc = await db["urlsV2"].find_one({"_id": blocked_id})
        assert doc is not None
        assert doc["status"] == "BLOCKED"
        assert doc["blocked_at"] == BLOCKED_AT
        assert doc["blocked_reason"] == BLOCKED_REASON
        assert doc["owner_id"] == user_a
        assert "creation_ip" not in doc
        assert "password" not in doc
    scrubbed_meta = await db["urlsV2"].find_one({"_id": blocked_default})
    assert "updated_ip" not in scrubbed_meta["meta_tags"]
    assert scrubbed_meta["meta_tags"]["title"] == "Preview title"

    assert await db["urlsV2"].find_one({"_id": active_default}) is None
    assert await db["urlsV2"].find_one({"_id": active_custom}) is None
    assert await db["urlsV2"].count_documents({"owner_id": user_a}) == 2

    assert await db["clicks"].count_documents({"meta.owner_id": user_a}) == 0
    assert (
        await db["clicks"].count_documents({"meta.url_id": {"$in": all_link_ids}}) == 0
    )
    assert await db["custom_domains"].count_documents({"owner_id": user_a}) == 0
    assert await db["tags"].count_documents({"owner_id": user_a}) == 0
    assert await db["users"].find_one({"_id": user_a}) is None


# ── Test B: bystander survival ───────────────────────────────────────────────


async def _snapshot_bystander(db, user_id: ObjectId, url_ids: list[ObjectId]) -> dict:
    return {
        "user": await db["users"].find_one({"_id": user_id}),
        "urls": await db["urlsV2"]
        .find({"owner_id": user_id})
        .sort("_id", 1)
        .to_list(length=None),
        "clicks": await db["clicks"]
        .find({"meta.url_id": {"$in": url_ids}})
        .sort("_id", 1)
        .to_list(length=None),
        "layouts": await db["page-layouts"]
        .find({"user_id": user_id})
        .sort("_id", 1)
        .to_list(length=None),
        "tokens": await db["verification-tokens"]
        .find({"user_id": user_id})
        .sort("_id", 1)
        .to_list(length=None),
    }


async def test_erase_leaves_bystander_documents_byte_identical(
    real_db, erasure_service
):
    db = real_db
    user_a = await insert_user(db, A_EMAIL)
    user_b = await insert_user(db, B_EMAIL)

    a_active = await insert_link(db, user_a, "axact1", "spoo.me")
    a_blocked = await insert_link(db, user_a, "axblk1", "spoo.me", blocked=True)
    await insert_click(db, a_active, user_a, "axact1", "spoo.me")
    await insert_click(db, a_blocked, user_a, "axblk1", "spoo.me")
    await insert_page_layout(db, user_a)
    await insert_verification_token(db, user_a, A_EMAIL)

    b_plain = await insert_link(db, user_b, "bplain1", "spoo.me")
    b_claimed = await insert_link(db, user_b, "bclaim1", "spoo.me", claimed=True)
    await insert_click(db, b_plain, user_b, "bplain1", "spoo.me")
    await insert_click(db, b_claimed, user_b, "bclaim1", "spoo.me")
    # Pre-claim click on the claimed link, still owner-stamped anonymous.
    await insert_click(db, b_claimed, ANONYMOUS_OWNER_ID, "bclaim1", "spoo.me")
    await insert_page_layout(db, user_b)
    await insert_verification_token(db, user_b, B_EMAIL)

    before = await _snapshot_bystander(db, user_b, [b_plain, b_claimed])
    assert before["user"] is not None
    assert len(before["urls"]) == 2
    assert len(before["clicks"]) == 3
    assert len(before["layouts"]) == 1
    assert len(before["tokens"]) == 1

    await make_purge_due(db, user_a)
    counts = await erasure_service.erase(user_a)
    assert counts["urlsV2"] == 1
    assert counts["urls_blocked_retained"] == 1
    assert counts["clicks"] == 2
    assert counts["page_layouts"] == 1
    assert counts["verification_tokens"] == 1

    after = await _snapshot_bystander(db, user_b, [b_plain, b_claimed])
    assert after == before

    # A is gone by Test A's rules: only the scrubbed BLOCKED link remains.
    assert await db["users"].find_one({"_id": user_a}) is None
    assert await db["urlsV2"].find_one({"_id": a_active}) is None
    retained = await db["urlsV2"].find_one({"_id": a_blocked})
    assert retained["status"] == "BLOCKED"
    assert "creation_ip" not in retained
    assert await db["clicks"].count_documents({"meta.owner_id": user_a}) == 0
    assert await db["page-layouts"].count_documents({"user_id": user_a}) == 0
    assert await db["verification-tokens"].count_documents({"email": A_EMAIL}) == 0
