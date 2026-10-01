"""
Lapse policies on the redirect path: what a cached link does once its owner
no longer holds a feature.

Pure: takes the link as the cache would serve it and the owner's resolved
entitlements, returns the link as the redirect must behave. Stored data is
never touched; a re-subscribe re-resolves from the document and the rules
come back.
"""

from __future__ import annotations

from infrastructure.cache.url_cache import UrlCacheData
from services.entitlements.resolver import Resolved


def apply_lapse_policies(data: UrlCacheData, resolved: Resolved) -> UrlCacheData:
    changes: dict = {"owner_ent_version": resolved.version}
    if data.geo_rules and not resolved.has("geo_targeting"):
        changes["geo_rules"] = None
    if data.ab_variants and not resolved.has("ab_variants"):
        changes["ab_variants"] = None
    if data.expired_redirect_url and not resolved.has("expired_fallback"):
        changes["expired_redirect_url"] = None
    if data.meta_title is not None and not resolved.has("custom_meta_tags"):
        changes.update(
            meta_title=None,
            meta_description=None,
            meta_image=None,
            meta_color=None,
            meta_image_width=None,
            meta_image_height=None,
        )
    return data.model_copy(update=changes)
