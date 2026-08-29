"""
Redirect routes — the hot path.

GET  /<short_code>          → resolve + redirect (rate-limit exempt)
POST /<short_code>/password → password form submission
"""

from __future__ import annotations

import time
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from dependencies import ClickSink, GeoIP, UrlSvc
from errors import (
    BlockedUrlError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    ValidationError,
)
from infrastructure.logging import get_logger, should_sample
from infrastructure.templates import templates
from middleware.error_handler import REDIRECT_EDGE_INTERCEPTED_STATUSES
from middleware.rate_limiter import Limits, limiter
from schemas.enums.domain_status import DomainStatus
from schemas.models.url import SchemaVersion
from services.click.bot_detection import should_block_bot, wants_preview
from services.click.events import ClickEvent
from services.meta_preview import build_preview_context
from shared.ip_utils import get_client_ip
from shared.url_utils import extract_hostname

log = get_logger(__name__)

router = APIRouter()

# Status-specific copy for the tenant fallback page. Maps the same status
# codes _error_page already emits today; keys must stay in sync.
_TENANT_ERROR_COPY = {
    "404": ("Not found", "This URL doesn't exist on {fqdn}."),
    "410": ("Expired", "This URL has expired and no longer redirects."),
    "451": (
        "Blocked",
        "This URL has been blocked for abuse. Blocks are reversible: contact "
        "the operator of {fqdn} if you think this is a mistake.",
    ),
    "403": ("Access denied", "You don't have permission to view this URL."),
}
_NOINDEX_HEADER = "noindex, nofollow, noarchive"

# Blocks are reversible and sometimes wrong; a blocked page that offers no
# route back is how a false positive becomes a complaint we never hear.
_APPEAL_COPY = "Think this is a mistake? Blocks are reversible."
_APPEAL_LINK = "Ask us to review it"

# Machine-readable slugs for the system-default error page so the edge can
# route on X-Error-Code. Only REDIRECT_EDGE_INTERCEPTED_STATUSES (defined
# next to the app-level set in middleware/error_handler.py) skip the body;
# 403 keeps its body always (bot blocks stay server-rendered) but still
# self-describes.
_ERROR_SLUGS = {
    "404": "not_found",
    "410": "gone",
    "451": "blocked",
    "403": "forbidden",
}


def _error_page(request: Request, code: str, message: str, status: int) -> Response:
    """Render the error page for a resolve/redirect failure.

    Custom-tenant requests get a self-contained minimal page (no external
    asset references — `spoo.ink/static/...` would 404 on a custom domain,
    leaving the marketing template unstyled). System-default requests keep
    the original branded ``error.html``.

    On 404, an ACTIVE tenant's ``not_found_redirect`` overrides the page
    entirely so owners control the UX for unknown paths consistently with
    the middleware-level disallowed-path branch.
    """
    tenant = getattr(request.state, "tenant", None)
    is_custom = tenant is not None and not tenant.is_system_default

    if is_custom and status == 404:
        active = tenant.status == DomainStatus.ACTIVE
        if active and tenant.not_found_redirect and request.method in {"GET", "HEAD"}:
            return RedirectResponse(
                tenant.not_found_redirect,
                status_code=302,
                headers={"X-Robots-Tag": _NOINDEX_HEADER},
            )

    if is_custom:
        title, body_tpl = _TENANT_ERROR_COPY.get(
            code, ("Error", "Something went wrong.")
        )
        return templates.TemplateResponse(
            request,
            "tenant_error.html",
            {
                "error_code": code,
                "error_title": title,
                "error_message": body_tpl.format(fqdn=tenant.fqdn),
            },
            status_code=status,
            headers={"X-Robots-Tag": _NOINDEX_HEADER},
        )

    slug = _ERROR_SLUGS.get(code)
    # settings can be absent (app built without lifespan) — default-off is
    # the fail-safe.
    settings = getattr(request.app.state, "settings", None)
    if (
        slug is not None
        and getattr(settings, "edge_composed_errors", False)
        and status in REDIRECT_EDGE_INTERCEPTED_STATUSES
        and request.method in {"GET", "HEAD"}
    ):
        # Caddy discards the body and composes the Next error page — skip
        # the template render on the hot path.
        return Response(status_code=status, headers={"X-Error-Code": slug})

    context = {
        "error_code": code,
        "error_message": message,
        "host_url": str(request.base_url),
    }
    if status == 451:
        context |= {
            "error_appeal": _APPEAL_COPY,
            "error_appeal_href": "/contact",
            "error_appeal_link": _APPEAL_LINK,
        }
    return templates.TemplateResponse(
        request,
        "error.html",
        context,
        status_code=status,
        headers={"X-Error-Code": slug} if slug is not None else None,
    )


@router.api_route("/{short_code}", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.exempt
async def redirect_url(
    short_code: str,
    request: Request,
    url_service: UrlSvc,
    click_sink: ClickSink,
    geoip: GeoIP,
) -> Response:
    """Resolve a short code and redirect to the destination URL.

    Rate-limit exempt — this is the hot path (~400k requests/day).
    """
    short_code = unquote(short_code)
    user_ip = get_client_ip(request)
    start_time = time.perf_counter()
    host_url = str(request.base_url)

    # 1. Resolve URL (cache-first)
    resolve_start = time.perf_counter()
    tenant = getattr(request.state, "tenant", None)
    domain = tenant.fqdn if tenant else None
    try:
        url_data, schema = await url_service.resolve(short_code, domain=domain)
    except NotFoundError:
        log.info("url_not_found", short_code=short_code)
        return _error_page(request, "404", "URL NOT FOUND", 404)
    except BlockedUrlError:
        log.info("url_blocked", short_code=short_code)
        return _error_page(request, "451", "THIS URL HAS BEEN BLOCKED", 451)
    except GoneError:
        log.info("url_gone", short_code=short_code)
        return _error_page(request, "410", "SHORT URL EXPIRED", 410)
    resolve_ms = int((time.perf_counter() - resolve_start) * 1000)

    # Custom meta-tags: preview crawlers get the owner's OG card instead of
    # the redirect; everyone else falls through to the 302. This runs before
    # the password gate (bots get the card, not the 401 page — it reveals
    # only owner-written text) and returns before the click emit — a preview
    # serve is never a click.
    user_agent = request.headers.get("User-Agent", "")
    if url_data.meta_title is not None and schema == SchemaVersion.V2:
        bot_param = "bot" in request.query_params
        if wants_preview(request.method, user_agent, bot_param=bot_param):
            log.info(
                "meta_preview_served",
                short_code=short_code,
                bot_param=bot_param,
            )
            resp = templates.TemplateResponse(
                request,
                "meta_preview.html",
                build_preview_context(url_data, auto_redirect=not bot_param),
                status_code=200,
            )
            resp.headers["X-Robots-Tag"] = _NOINDEX_HEADER
            return resp

    # 2. Password check
    if url_data.password_hash:
        password = request.query_params.get("password")
        if not url_data.verify_password(password):
            log.debug("url_password_required", short_code=short_code, schema=schema)
            return templates.TemplateResponse(
                request,
                "password.html",
                {"short_code": short_code, "host_url": host_url},
                status_code=401,
            )

    # 3. Geo-targeting decision — must run before the click event is built
    #    so the routing decision rides the event. CF-IPCountry is free
    #    (CF-injected, trustworthy behind the tunnel/proxy); the mmdb lookup
    #    only fires for geo links when the header is absent (self-host).
    destination = url_data.long_url
    resolved_country: str | None = None
    geo_matched = False
    if url_data.geo_rules:
        resolved_country = (
            request.headers.get("CF-IPCountry") or ""
        ).strip().upper() or None
        if resolved_country is None:
            resolved_country = await geoip.get_country_code(user_ip)
        if resolved_country and resolved_country in url_data.geo_rules:
            destination = url_data.geo_rules[resolved_country]
            geo_matched = True

    # 4. Pre-emit bot block — the DECISION must run before the redirect is
    #    served (click processing may happen out-of-band); bot metadata
    #    RECORDING stays in the click pipeline.
    if should_block_bot(request.method, user_agent, url_data, schema):
        log.info("click_tracking_bot_blocked", short_code=short_code, schema=schema)
        return _error_page(request, "403", "ACCESS DENIED", 403)

    # 5. Emit click event — skip for HEAD / OPTIONS
    tracking_ms = 0
    if request.method not in ("HEAD", "OPTIONS"):
        referrer = request.headers.get("Referer")
        cf_city = request.headers.get("CF-IPCity")
        is_emoji = schema == SchemaVersion.EMOJI
        tracking_start = time.perf_counter()
        event = ClickEvent(
            # The RESOLVED identity, not the request-path form: emoji codes
            # can resolve through a byte-variant (VS16) of the stored alias,
            # and the click handlers key their writes — legacy _id updates,
            # v2 max-clicks cache invalidation — off this field. The raw
            # form would silently drop those writes.
            short_code=url_data.alias,
            schema_key=schema,
            is_emoji=is_emoji,
            # ClickEvent strips url.password_hash on construction (v1 hashes
            # are plaintext) — no producer-side sanitization needed.
            url=url_data,
            client_ip=user_ip,
            user_agent=user_agent,
            referrer=referrer,
            cf_city=cf_city,
            resolved_country=resolved_country,
            geo_matched=geo_matched,
            # Raw capture only — ClickEvent sanitises and bounds these
            # structurally, same as the password-hash strip.
            utm_source=request.query_params.get("utm_source"),
            utm_medium=request.query_params.get("utm_medium"),
            utm_campaign=request.query_params.get("utm_campaign"),
            redirect_ms=int((time.perf_counter() - start_time) * 1000),
        )
        try:
            await click_sink.emit(event)
        except ValidationError:
            # Bad / missing User-Agent — skip analytics, still redirect
            log.info(
                "click_tracking_validation_error", short_code=short_code, schema=schema
            )
        except ForbiddenError as exc:
            # Inline sink, defense in depth: the legacy handler blocked a
            # bot the pre-emit check missed — block the redirect as before
            log.info(
                "click_tracking_bot_blocked", short_code=short_code, reason=str(exc)
            )
            return _error_page(request, "403", "ACCESS DENIED", 403)
        except Exception:
            log.exception("click_tracking_failed", short_code=short_code, schema=schema)
        tracking_ms = int((time.perf_counter() - tracking_start) * 1000)

    # 6. Redirect
    total_ms = int((time.perf_counter() - start_time) * 1000)
    if should_sample("url_redirect"):
        log.info(
            "url_redirect",
            short_code=short_code,
            schema=schema,
            resolve_ms=resolve_ms,
            tracking_ms=tracking_ms,
            total_ms=total_ms,
            long_url_domain=extract_hostname(url_data.long_url),
            password_protected=bool(getattr(url_data, "password_hash", None)),
            had_max_clicks=bool(getattr(url_data, "max_clicks", None)),
            max_clicks=getattr(url_data, "max_clicks", None),
            owner_id=str(getattr(url_data, "owner_id", "")) or None,
            geo_targeted=bool(url_data.geo_rules),
            resolved_country=resolved_country,
            geo_matched=geo_matched,
            slow=total_ms > 100,
        )
    resp = RedirectResponse(destination, status_code=302)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if url_data.geo_rules:
        # Response varies by visitor country — no intermediary may cache it
        resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/{short_code}/password", include_in_schema=False)
@limiter.limit(Limits.PASSWORD_CHECK)
async def check_password(
    short_code: str,
    request: Request,
    url_service: UrlSvc,
) -> Response:
    """Verify a password for a password-protected URL.

    On success: redirect to /<short_code>?password=<password>.
    On failure: re-render password.html with error message.
    """
    short_code = unquote(short_code)
    form_data = await request.form()
    password = form_data.get("password")
    host_url = str(request.base_url)

    tenant = getattr(request.state, "tenant", None)
    domain = tenant.fqdn if tenant else None
    try:
        url_data, _schema = await url_service.resolve(short_code, domain=domain)
    except (NotFoundError, BlockedUrlError, ForbiddenError, GoneError):
        return _error_page(
            request, "400", "Invalid short code or URL not password-protected", 400
        )

    if not url_data.password_hash:
        return _error_page(
            request, "400", "Invalid short code or URL not password-protected", 400
        )

    if url_data.verify_password(password):
        log.info("url_password_verified", short_code=short_code)
        return RedirectResponse(f"/{short_code}?password={password}", status_code=302)

    # Wrong password — re-render password form with error
    log.info("url_password_incorrect", short_code=short_code)
    return templates.TemplateResponse(
        request,
        "password.html",
        {"short_code": short_code, "error": "Incorrect password", "host_url": host_url},
    )
