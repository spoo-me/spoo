"""API v1 routes package."""

from fastapi import APIRouter

from routes.api_v1 import (
    apps,
    billing,
    bulk,
    custom_domains,
    domain_intel,
    emoji,
    expand,
    exports,
    keys,
    management,
    me,
    metadata,
    plans,
    public_preview,
    public_stats,
    reports,
    shorten,
    stats,
    tags,
    urls,
    webhooks,
)

router = APIRouter(prefix="/api/v1")
router.include_router(shorten.router)
router.include_router(emoji.router)
router.include_router(urls.router)
router.include_router(bulk.router)
router.include_router(tags.router)
router.include_router(management.router)
router.include_router(stats.router)
router.include_router(public_stats.router)
router.include_router(exports.router)
router.include_router(keys.router)
router.include_router(apps.router)
router.include_router(custom_domains.router)
router.include_router(metadata.router)
router.include_router(expand.router)
router.include_router(domain_intel.router)
router.include_router(me.router)
router.include_router(plans.router)
router.include_router(billing.router)
router.include_router(public_preview.router)
router.include_router(reports.router)
router.include_router(webhooks.router)
