"""MongoDB index + collection setup. Idempotent.

Data migrations live under ``infrastructure/bootstrap/`` and must run before
this — see lifespan ordering in app.py.
"""

from __future__ import annotations

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import CollectionInvalid, OperationFailure

from infrastructure.logging import get_logger

log = get_logger(__name__)


async def ensure_indexes(
    db: AsyncDatabase, *, webhook_log_ttl_seconds: int = 2_592_000
) -> None:
    users_col = db["users"]
    urls_v2_col = db["urlsV2"]
    urls_legacy_col = db["urls"]
    emojis_col = db["emojis"]
    clicks_col = db["clicks"]
    api_keys_col = db["api-keys"]
    page_layouts_col = db["page-layouts"]
    tokens_col = db["verification-tokens"]

    # ── users ──────────────────────────────────────────────────────────────
    await users_col.create_index([("email", 1)], unique=True)
    await users_col.create_index(
        [
            ("auth_providers.provider", 1),
            ("auth_providers.provider_user_id", 1),
        ],
        unique=True,
        sparse=True,
    )
    await users_col.create_index([("auth_providers.provider", 1)])

    # ── urlsV2 ─────────────────────────────────────────────────────────────
    # Per-domain alias namespace via compound unique. Replaces the legacy
    # global ``alias_1`` unique which would collide same-alias-different-domain
    # once custom domains land.
    await urls_v2_col.create_index([("domain", 1), ("alias", 1)], unique=True)
    # Drop the obsolete global unique left over from pre-PR1 deploys.
    # Code 27 = IndexNotFound — expected on every boot after the first run,
    # so log only the actual-drop case to keep steady-state logs quiet.
    try:
        await urls_v2_col.drop_index("alias_1")
        log.info("legacy_alias_index_dropped", index="alias_1")
    except OperationFailure as e:
        if getattr(e, "code", None) != 27:
            raise
    await urls_v2_col.create_index([("owner_id", 1)])
    await urls_v2_col.create_index([("owner_id", 1), ("created_at", -1)])
    await urls_v2_col.create_index([("total_clicks", -1)])
    await urls_v2_col.create_index([("last_click", -1)])
    # Claimed-set lookup for scope=ALL stats. Partial — holds only claimed
    # links, so it stays sub-ms at any account size. Relies on claimed_at
    # being ABSENT (not null) on unclaimed docs; the create path strips it.
    await urls_v2_col.create_index(
        [("owner_id", 1)],
        name="owner_claimed",
        partialFilterExpression={"claimed_at": {"$exists": True}},
    )

    # ── safety_verdicts ────────────────────────────────────────────────────
    # One verdict per destination host; sweeps pivot on registrable domain.
    verdicts_col = db["safety_verdicts"]
    await verdicts_col.create_index([("host", 1)], unique=True)
    await verdicts_col.create_index([("registrable_domain", 1)])
    await verdicts_col.create_index([("updated_at", -1)])

    # ── safety_feed_domains ────────────────────────────────────────────────
    # Membership checks ride _id ("<feed>:<domain>"); this index serves the
    # per-feed stale purge on sync and per-feed counts.
    feed_domains_col = db["safety_feed_domains"]
    await feed_domains_col.create_index([("feed", 1), ("synced_at", 1)])

    # ── destination decomposition (url-safety) ─────────────────────────────
    # Sparse: unparseable destinations are `dest: null` (backfill) or absent
    # (create/edit); neither yields the nested paths, so sparse excludes both.
    for _url_col in (urls_v2_col, urls_legacy_col, emojis_col):
        await _url_col.create_index(
            [("dest.registrable_domain", 1)], name="dest_registrable", sparse=True
        )
        # Every enforcement query is an equality match on dest.host.
        await _url_col.create_index([("dest.host", 1)], name="dest_host", sparse=True)

    # ── clicks (time-series) ───────────────────────────────────────────────
    # Create the time-series collection if it doesn't exist yet.
    # Already-existing collection raises OperationFailure — swallow it.
    try:
        await db.create_collection(
            "clicks",
            timeseries={
                "timeField": "clicked_at",
                "metaField": "meta",
                "granularity": "seconds",
            },
        )
    except (CollectionInvalid, OperationFailure) as e:
        # Error code 48 = NamespaceExists (collection already exists) — expected on every
        # boot after the first. Any other OperationFailure (permissions, bad options, etc.)
        # is a real problem and should propagate.
        if isinstance(e, CollectionInvalid) or getattr(e, "code", None) == 48:
            pass
        else:
            raise

    await clicks_col.create_index([("meta.url_id", 1), ("clicked_at", -1)])
    await clicks_col.create_index([("clicked_at", -1)])
    # CRITICAL: for user-level analytics (account-scoped stats queries)
    await clicks_col.create_index([("meta.owner_id", 1), ("clicked_at", -1)])
    # for the short_code stats filter
    await clicks_col.create_index([("meta.short_code", 1), ("clicked_at", -1)])
    # sparse — older buckets have no meta.domain, index stays small
    await clicks_col.create_index([("meta.domain", 1), ("clicked_at", -1)], sparse=True)

    # ── api-keys ───────────────────────────────────────────────────────────
    await api_keys_col.create_index([("user_id", 1)])
    await api_keys_col.create_index([("token_hash", 1)], unique=True)
    await api_keys_col.create_index([("expires_at", 1)], expireAfterSeconds=0)
    await page_layouts_col.create_index([("user_id", 1), ("page", 1)], unique=True)

    # ── verification-tokens ────────────────────────────────────────────────
    await tokens_col.create_index([("user_id", 1)])
    await tokens_col.create_index([("token_hash", 1)])
    await tokens_col.create_index([("token_type", 1)])
    await tokens_col.create_index([("expires_at", 1)], expireAfterSeconds=0)
    await tokens_col.create_index(
        [("user_id", 1), ("token_type", 1), ("used_at", 1), ("created_at", -1)],
        name="ix_latest_unused_by_user",
    )

    # ── app-grants ─────────────────────────────────────────────────────
    app_grants_col = db["app-grants"]
    await app_grants_col.create_index([("user_id", 1), ("app_id", 1)], unique=True)
    await app_grants_col.create_index([("user_id", 1), ("revoked_at", 1)])
    await app_grants_col.create_index([("app_id", 1), ("revoked_at", 1)])

    # ── feature-flags ──────────────────────────────────────────────────
    feature_flags_col = db["feature_flags"]
    await feature_flags_col.create_index([("name", 1)], unique=True)

    # ── reports ────────────────────────────────────────────────────────
    # One doc per reported (domain, code) — domain is null for the system
    # default, so the compound unique still keys correctly. Velocity
    # triage reads sort by last_reported_at; the funnel filters on status.
    reports_col = db["reports"]
    await reports_col.create_index([("domain", 1), ("code", 1)], unique=True)
    await reports_col.create_index([("last_reported_at", -1)])
    await reports_col.create_index([("status", 1)])

    # ── report_submissions ─────────────────────────────────────────────
    report_submissions_col = db["report_submissions"]
    await report_submissions_col.create_index([("created_at", -1)])

    # ── custom_domains ────────────────────────────────────────────────
    custom_domains_col = db["custom_domains"]
    # Partial unique on fqdn — REVOKED docs preserved for audit don't reserve
    # the fqdn. DCV at register-time is the takeover gate.
    try:
        await custom_domains_col.drop_index("fqdn_1")
        log.info("legacy_custom_domain_fqdn_index_dropped", index="fqdn_1")
    except OperationFailure as e:
        if getattr(e, "code", None) != 27:
            raise
    await custom_domains_col.create_index(
        [("fqdn", 1)],
        unique=True,
        partialFilterExpression={
            "status": {"$in": ["pending", "verifying", "active", "suspended"]}
        },
        name="fqdn_unique_non_revoked",
    )
    await custom_domains_col.create_index([("owner_id", 1), ("created_at", -1)])
    await custom_domains_col.create_index([("status", 1), ("last_verified_at", 1)])

    # ── webhooks ───────────────────────────────────────────────────────
    # webhook-events: the fact stored once (deliveries reference it).
    # Both TTLs ride the same value so a delivery can never outlive its
    # event — changing WEBHOOKS_DELIVERY_LOG_TTL_DAYS requires
    # the drop-recreate below because Mongo rejects expireAfterSeconds
    # changes on an existing TTL index.
    webhook_events_col = db["webhook-events"]
    await webhook_events_col.create_index([("event_id", 1)], unique=True)
    await webhook_events_col.create_index([("owner_id", 1), ("created_at", -1)])
    await _ensure_ttl_index(webhook_events_col, webhook_log_ttl_seconds)

    webhook_endpoints_col = db["webhook-endpoints"]
    await webhook_endpoints_col.create_index([("user_id", 1), ("status", 1)])
    await webhook_endpoints_col.create_index(
        [("user_id", 1), ("events", 1), ("status", 1)], name="ix_matcher"
    )

    webhook_deliveries_col = db["webhook-deliveries"]
    # THE claim index — the executor's whole query shape.
    await webhook_deliveries_col.create_index(
        [("status", 1), ("next_attempt_at", 1)], name="ix_claim"
    )
    await webhook_deliveries_col.create_index([("endpoint_id", 1), ("created_at", -1)])
    await webhook_deliveries_col.create_index([("endpoint_id", 1), ("status", 1)])
    await webhook_deliveries_col.create_index([("user_id", 1), ("created_at", -1)])
    await webhook_deliveries_col.create_index([("webhook_id", 1)], unique=True)
    await _ensure_ttl_index(webhook_deliveries_col, webhook_log_ttl_seconds)

    # ── scheduled_tasks ────────────────────────────────────────────────────
    # THE claim index — the task runner's whole query shape (same pattern
    # as the webhook executor above). _id is the task name.
    scheduled_tasks_col = db["scheduled_tasks"]
    await scheduled_tasks_col.create_index(
        [("enabled", 1), ("next_run_at", 1)], name="ix_claim"
    )

    log.info("mongodb_indexes_ensured")


async def _ensure_ttl_index(col, expire_after_seconds: int) -> None:
    """Create the created_at TTL index, recreating it when the configured
    TTL changed (Mongo rejects expireAfterSeconds edits in create_index)."""
    try:
        await col.create_index(
            [("created_at", 1)],
            expireAfterSeconds=expire_after_seconds,
            name="ttl_created_at",
        )
    except OperationFailure as e:
        if getattr(e, "code", None) != 85:  # IndexOptionsConflict
            raise
        try:
            await col.drop_index("ttl_created_at")
        except OperationFailure as drop_err:
            # Code 27 = IndexNotFound: a racing instance (rolling deploy)
            # already dropped it — recreating below is still correct.
            if getattr(drop_err, "code", None) != 27:
                raise
        await col.create_index(
            [("created_at", 1)],
            expireAfterSeconds=expire_after_seconds,
            name="ttl_created_at",
        )
        log.info("webhook_ttl_index_recreated", collection=col.name)
