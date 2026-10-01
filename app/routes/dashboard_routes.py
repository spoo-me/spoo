"""
Dashboard routes — template-rendering pages and profile picture API.

GET  /dashboard             → redirect to /dashboard/links
GET  /dashboard/links       → links management page
GET  /dashboard/keys        → API keys page
GET  /dashboard/statistics  → statistics page
GET  /dashboard/settings    → settings page
GET  /dashboard/billing     → billing page
GET  /dashboard/apps          → connected apps + ecosystem
GET  /dashboard/profile-pictures  → available pictures (JSON, legacy)
POST /dashboard/profile-pictures  → set profile picture (JSON, legacy)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.dependencies import (
    AppGrantRepo,
    AppRegistryDep,
    CurrentUser,
    JwtUser,
    OptionalUser,
    ProfilePictureSvc,
)
from app.infrastructure.logging import get_logger
from app.infrastructure.templates import templates
from app.middleware.rate_limiter import Limits, limiter
from app.schemas.dto.requests.profile_pictures import SetProfilePictureRequest
from app.schemas.dto.responses.profile_pictures import (
    AvailablePicturesResponse,
    ProfilePictureMessageResponse,
)
from app.schemas.models.app import AppStatus
from app.services.profile_picture_service import ProfilePictureService

log = get_logger(__name__)

router = APIRouter(prefix="/dashboard", include_in_schema=False)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _unauth_redirect() -> Response:
    return RedirectResponse("/?login=true", status_code=302)


async def _render_dashboard_page(
    template_name: str,
    request: Request,
    user: CurrentUser | None,
    svc: ProfilePictureService,
) -> Response:
    """Shared logic for all dashboard page routes — auth check + profile fetch + render."""
    if user is None:
        return _unauth_redirect()
    profile = await svc.get_dashboard_profile(user.user_id)
    flag_svc = getattr(request.app.state, "feature_flag_service", None)
    show_custom_domains = False
    if flag_svc is not None:
        try:
            show_custom_domains = await flag_svc.is_enabled("custom_domains", user)
        except Exception as exc:
            log.warning("dashboard_flag_lookup_failed", error=str(exc))

    # User's ACTIVE custom domains for the shared domain-picker partial.
    # Fetched here (rendered server-side) so the modal/filter chip never
    # flickers a loading state on first paint. Skipped entirely when the
    # flag is off — saves a Mongo round-trip on the hot dashboard path.
    user_domains: list[dict[str, str]] = []
    if show_custom_domains:
        cd_svc = getattr(request.app.state, "custom_domain_service", None)
        if cd_svc is not None:
            try:
                from app.schemas.dto.requests.custom_domain import (
                    ListCustomDomainsQuery,
                )
                from app.schemas.enums.domain_status import DomainStatus

                items, _ = await cd_svc.list_by_owner(
                    user, ListCustomDomainsQuery(page=1, page_size=100)
                )
                user_domains = [
                    {"fqdn": d.fqdn, "id": str(d.id)}
                    for d in items
                    if d.status == DomainStatus.ACTIVE
                ]
            except Exception as exc:
                log.warning("dashboard_user_domains_fetch_failed", error=str(exc))

    # The domain picker's "DEFAULT" item must use the same fqdn the service
    # writes to `urls.domain` for system-default URLs. In dev these diverge
    # from the request host (settings=spoo.local, base_url=localhost:8000)
    # so deriving from host_url alone breaks the `is-selected` highlight on
    # default-domain URLs. Pull the actual setting instead.
    settings = getattr(request.app.state, "settings", None)
    system_default_domain = (
        settings.system_default_domain if settings is not None else None
    )

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "host_url": str(request.base_url),
            "user": profile,
            "show_custom_domains": show_custom_domains,
            "user_domains": user_domains,
            "system_default_domain": system_default_domain,
        },
    )


# ── Page routes ──────────────────────────────────────────────────────────────


@router.get("")
@router.get("/")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_root(
    request: Request,
    user: OptionalUser,
) -> Response:
    if user is None:
        return _unauth_redirect()
    return RedirectResponse("/dashboard/links", status_code=302)


@router.get("/links")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_links(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
) -> Response:
    return await _render_dashboard_page("dashboard/links.html", request, user, svc)


@router.get("/keys")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_keys(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
) -> Response:
    return await _render_dashboard_page("dashboard/keys.html", request, user, svc)


@router.get("/domains")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_domains(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
) -> Response:
    if user is None:
        return _unauth_redirect()
    # Direct-URL guard: non-allowlisted users get bounced to /dashboard/links
    # instead of seeing an empty/teaser page. Sidebar nav is hidden the same way.
    flag_svc = getattr(request.app.state, "feature_flag_service", None)
    if flag_svc is not None and not await flag_svc.is_enabled("custom_domains", user):
        return RedirectResponse("/dashboard/links", status_code=302)
    return await _render_dashboard_page("dashboard/domains.html", request, user, svc)


@router.get("/statistics")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_statistics(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
) -> Response:
    return await _render_dashboard_page("dashboard/statistics.html", request, user, svc)


@router.get("/settings")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_settings(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
) -> Response:
    return await _render_dashboard_page("dashboard/settings.html", request, user, svc)


@router.get("/billing")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_billing(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
) -> Response:
    return await _render_dashboard_page("dashboard/billing.html", request, user, svc)


@router.get("/apps")
@limiter.limit(Limits.DASHBOARD_READ)
async def dashboard_apps(
    request: Request,
    user: OptionalUser,
    svc: ProfilePictureSvc,
    grant_repo: AppGrantRepo,
    app_registry: AppRegistryDep,
) -> Response:
    if user is None:
        return _unauth_redirect()
    has_live_apps = any(app.status == AppStatus.LIVE for app in app_registry.values())
    if not has_live_apps:
        return RedirectResponse("/dashboard", status_code=302)

    profile = await svc.get_dashboard_profile(user.user_id)
    grants = await grant_repo.find_active_for_user(user.user_id)
    grant_map = {g.app_id: g for g in grants}

    connected: list[dict] = []
    available: list[dict] = []
    coming_soon: list[dict] = []

    for app_id, app in app_registry.items():
        entry = {"app_id": app_id, **app.model_dump()}
        if app_id in grant_map:
            entry["grant"] = grant_map[app_id]
            connected.append(entry)
        elif app.status == AppStatus.COMING_SOON:
            coming_soon.append(entry)
        else:
            available.append(entry)

    return templates.TemplateResponse(
        request,
        "dashboard/apps.html",
        {
            "host_url": str(request.base_url),
            "user": profile,
            "connected": connected,
            "available": available,
            "coming_soon": coming_soon,
        },
    )


# ── Profile pictures (JSON API, legacy) ──────────────────────────────────────
# Canonical home: /api/v1/me/profile-pictures (routes/api_v1/me.py). This pair
# exists only for the legacy Jinja dashboard; drop it with the Jinja pages.


@router.get("/profile-pictures")
@limiter.limit(Limits.DASHBOARD_READ)
async def get_profile_pictures(
    request: Request,
    user: JwtUser,
    svc: ProfilePictureSvc,
) -> AvailablePicturesResponse:
    pictures = await svc.get_available_pictures(user.user_id)
    return AvailablePicturesResponse(pictures=pictures)


@router.post("/profile-pictures")
@limiter.limit(Limits.PROFILE_PICTURE_SET)
async def set_profile_picture(
    request: Request,
    body: SetProfilePictureRequest,
    user: JwtUser,
    svc: ProfilePictureSvc,
) -> ProfilePictureMessageResponse:
    await svc.set_picture(user.user_id, body.picture_id)
    return ProfilePictureMessageResponse(message="Profile picture updated successfully")
