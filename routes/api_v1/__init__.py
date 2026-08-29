"""API v1 routes package."""

from fastapi import APIRouter

from routes.api_v1 import (
    apps,
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
    public_preview,
    public_stats,
    reports,
    shorten,
    stats,
    urls,
    webhooks,
)

router = APIRouter(prefix="/api/v1")
router.include_router(shorten.router)
router.include_router(emoji.router)
router.include_router(urls.router)
router.include_router(bulk.router)
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
router.include_router(public_preview.router)
router.include_router(reports.router)
router.include_router(webhooks.router)
