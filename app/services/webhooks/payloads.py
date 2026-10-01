"""Payload builders — internal facts → public webhook payloads.

The one place internal shapes (ClickEvent, UrlV2Doc) are projected into
the payloads the registry documents. Privacy inherits structurally from
the sources (ClickEvent strips password material and sanitizes UTM);
this module additionally guarantees no raw IP and no password hashes
ever reach a payload.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ua_parser import parse as ua_parse

from app.infrastructure.geoip import GeoIPService
from app.schemas.models.base import ANONYMOUS_OWNER_ID
from app.schemas.models.url import LinkMetaTags, UrlStatus, UrlV2Doc
from app.services.click.bot_detection import get_bot_name, is_bot_request
from app.services.click.events import ClickEvent
from app.services.click.handlers import classify_device
from app.services.events.contract import DomainEvent

# Raw UA is visitor-controlled input — bound and strip control chars
# before it rides the wire (same posture as ClickEvent's UTM bound).
_UA_MAX_LEN = 512
_UA_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _wire_user_agent(user_agent: str) -> str | None:
    cleaned = _UA_CONTROL_CHARS_RE.sub("", user_agent).strip()[:_UA_MAX_LEN]
    return cleaned or None


def short_url_of(domain: str, alias: str) -> str:
    return f"https://{domain}/{alias}"


def _public_meta_tags(meta: LinkMetaTags | None) -> dict[str, Any] | None:
    """Public projection of meta tags — updated_ip/updated_at/image_meta
    are internal bookkeeping and never ride the wire."""
    if meta is None:
        return None
    return {k: getattr(meta, k, None) for k in _META_TAGS_PUBLIC_FIELDS}


def link_snapshot(doc: UrlV2Doc) -> dict[str, Any]:
    """The public snapshot carried by every link.* lifecycle event.

    A projection of the link DOCUMENT, never of entitlements: feature
    fields (geo_rules, ab_variants, meta_tags) are null when unset, which needs no
    flag lookup — flags gate writes, the doc already carries the truth.
    """
    return {
        "link_id": str(doc.id),
        "alias": doc.alias,
        "domain": doc.domain,
        "short_url": short_url_of(doc.domain, doc.alias),
        "long_url": doc.long_url,
        "status": doc.status.value,
        "password_protected": doc.password is not None,
        "block_bots": bool(doc.block_bots),
        "max_clicks": doc.max_clicks,
        "expires_at": doc.expire_after.isoformat() if doc.expire_after else None,
        "starts_at": doc.starts_at.isoformat() if doc.starts_at else None,
        "pre_start_url": doc.pre_start_url,
        "geo_rules": doc.geo_rules or None,
        "tag_ids": [str(i) for i in doc.tag_ids],
        "ab_variants": (
            [v.model_dump() for v in doc.ab_variants] if doc.ab_variants else None
        ),
        "meta_tags": _public_meta_tags(doc.meta_tags),
        "total_clicks": doc.total_clicks,
        "created_at": doc.created_at.isoformat(),
    }


def link_owner_id(doc: UrlV2Doc) -> str | None:
    """Anonymous links have no possible subscriber — producers skip emit."""
    if doc.owner_id == ANONYMOUS_OWNER_ID:
        return None
    return str(doc.owner_id)


_META_TAGS_PUBLIC_FIELDS = ("title", "description", "image", "color")


def _event_change_value(field_name: str, value: object) -> object:
    """Project one changed value into its public wire form.

    Secrets and internal bookkeeping are stripped structurally here so
    every producer of the changes map inherits the guarantee: password
    material never rides the backbone, meta_tags keep only their public
    fields (``updated_ip``/``updated_at``/``image_meta`` are internal),
    and datetimes are ISO-8601 like every other timestamp on the wire.
    """
    if value is None:
        return None
    if isinstance(value, UrlStatus):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if field_name == "tag_ids" and isinstance(value, list):
        return [str(v) for v in value]
    if field_name == "meta_tags":
        if isinstance(value, LinkMetaTags):
            return _public_meta_tags(value)
        return {k: dict(value).get(k) for k in _META_TAGS_PUBLIC_FIELDS}
    return value


# Internal field name → the public snapshot vocabulary. One resource,
# one vocabulary: a consumer must never meet two names for one field.
_CHANGE_KEY_PUBLIC = {"expire_after": "expires_at"}


def event_changes(existing: UrlV2Doc, update_ops: dict) -> dict:
    """update_ops → the public ``changes`` map for link.updated. Shared by
    the single-item and bulk producers so the wire shape cannot fork.
    Keys speak the snapshot's public names, never Mongo field names."""
    changes: dict = {}
    for field_name, new_value in update_ops.items():
        if field_name == "updated_at":
            continue
        old_value = getattr(existing, field_name, None)
        if field_name == "password":
            # Presence booleans only — hashes must never ride the backbone
            # (same posture as ClickEvent's password strip).
            old_value = old_value is not None
            new_value = new_value is not None
            field_name = "password_protected"
        public_name = _CHANGE_KEY_PUBLIC.get(field_name, field_name)
        changes[public_name] = {
            "old": _event_change_value(field_name, old_value),
            "new": _event_change_value(field_name, new_value),
        }
    return changes


def build_link_expired(doc: UrlV2Doc, reason: str) -> DomainEvent | None:
    """link.expired fires at DISCOVERY time: the max-clicks branch of the
    click handler, or the redirect path's lazy time-expiry flip. Both are
    once-per-link (atomic conditional updates gate the emit)."""
    owner = link_owner_id(doc)
    if owner is None:
        return None
    return DomainEvent(
        type="link.expired",
        owner_id=owner,
        data={"link": link_snapshot(doc), "reason": reason},
    )


async def build_link_clicked(
    event: ClickEvent,
    system_default_domain: str,
    geoip: GeoIPService | None = None,
) -> DomainEvent | None:
    """Adapt a ClickEvent (events:clicks wire) to a link.clicked DomainEvent.

    Returns None for unowned links — subscriptions are per-owner, so
    anonymous/v1 clicks have no possible subscriber and skip the
    (comparatively expensive) UA parse entirely.

    Async because the geoip fallback lookup is async; every caller is
    already in an async context (the webhooks consumer and the inline
    fanout sink).
    """
    owner_id = event.url.owner_id
    if owner_id is None or owner_id == str(ANONYMOUS_OWNER_ID):
        return None

    ua = ua_parse(event.user_agent)
    browser = ua.user_agent.family if ua.user_agent else None
    os_family = ua.os.family if ua.os else None
    device = classify_device(ua, event.user_agent)
    bot = is_bot_request(event.user_agent)

    # Geo links carry the routing decision; everything else resolves the
    # same mmdb the stats consumer uses. IP itself never leaves this frame.
    country = event.resolved_country
    if country is None and geoip is not None:
        country = await geoip.get_country_code(event.client_ip)

    domain = event.url.domain or system_default_domain
    return DomainEvent(
        type="link.clicked",
        owner_id=owner_id,
        occurred_at=event.enqueued_at,
        data={
            "link_id": event.url.id,
            "alias": event.short_code,
            "domain": domain,
            "short_url": short_url_of(domain, event.short_code),
            "long_url": event.url.long_url,
            "clicked_at": event.enqueued_at.isoformat(),
            "country": country,
            "city": event.cf_city,
            "browser": browser,
            "os": os_family,
            "device": device,
            "user_agent": _wire_user_agent(event.user_agent),
            "referrer": event.referrer,
            "utm": {
                "source": event.utm_source,
                "medium": event.utm_medium,
                "campaign": event.utm_campaign,
            },
            "is_bot": bot,
            "bot_name": get_bot_name(event.user_agent) if bot else None,
            "total_clicks": event.url.total_clicks,
        },
    )
