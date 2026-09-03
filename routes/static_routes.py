"""
Static, SEO, legal, contact, and report routes.

SEO:     robots.txt, sitemap.xml, humans.txt, security.txt, favicon.ico
Docs:    /docs → external redirect, privacy policy, terms of service
Contact: GET/POST /contact, GET/POST /report
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from dependencies import ContactSvc, Settings, UrlSvc
from errors import AppError, ForbiddenError, ValidationError
from infrastructure.logging import get_logger
from infrastructure.templates import templates
from middleware.rate_limiter import Limits, limiter
from shared.ip_utils import get_client_ip

log = get_logger(__name__)

router = APIRouter(include_in_schema=False)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
_MISC_DIR = os.path.join(_PROJECT_ROOT, "misc")
_STATIC_DIR = os.path.join(_PROJECT_ROOT, "static")


# ── SEO files ────────────────────────────────────────────────────────────────


@router.get("/robots.txt")
@limiter.exempt
async def robots(request: Request) -> Response:
    return FileResponse(os.path.join(_MISC_DIR, "robots.txt"), media_type="text/plain")


@router.get("/sitemap.xml")
@limiter.exempt
async def sitemap(request: Request) -> Response:
    return FileResponse(
        os.path.join(_MISC_DIR, "sitemap.xml"), media_type="application/xml"
    )


@router.get("/humans.txt")
@limiter.exempt
async def humans(request: Request) -> Response:
    return FileResponse(os.path.join(_MISC_DIR, "humans.txt"), media_type="text/plain")


@router.get("/security.txt")
@limiter.exempt
async def security(request: Request) -> Response:
    return FileResponse(
        os.path.join(_MISC_DIR, "security.txt"), media_type="text/plain"
    )


@router.get("/favicon.ico")
@limiter.exempt
async def favicon(request: Request) -> Response:
    return FileResponse(
        os.path.join(_STATIC_DIR, "images", "favicon.ico"),
        media_type="image/x-icon",
    )


# ── Docs / legal ─────────────────────────────────────────────────────────────

# docs.spoo.me still resolves but 301s here, so pointing at it costs a hop.
DOCS_ROOT = "https://spoo.me/docs"


@router.get("/api")
@limiter.exempt
async def api_redirect(request: Request) -> Response:
    return RedirectResponse(DOCS_ROOT + "/introduction", status_code=301)


@router.get("/docs")
@router.get("/docs/")
@limiter.exempt
async def docs_redirect(request: Request) -> Response:
    return RedirectResponse(DOCS_ROOT, status_code=301)


@router.get("/docs/privacy-policy")
@router.get("/legal/privacy-policy")
@router.get("/privacy-policy")
@router.get("/privacy")
@limiter.exempt
async def privacy_policy(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "legal/privacy-policy.html",
        {"host_url": str(request.base_url)},
    )


@router.get("/docs/terms-of-service")
@router.get("/docs/tos")
@router.get("/legal/terms-of-service")
@router.get("/legal/tos")
@router.get("/tos")
@router.get("/terms-of-service")
@limiter.exempt
async def terms_of_service(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "legal/terms-of-service.html",
        {"host_url": str(request.base_url)},
    )


@router.get("/docs/refund-policy")
@router.get("/legal/refund-policy")
@router.get("/refund-policy")
@router.get("/refund")
@limiter.exempt
async def refund_policy(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "legal/refund-policy.html",
        {"host_url": str(request.base_url)},
    )


@router.get("/docs/acceptable-use-policy")
@router.get("/legal/acceptable-use-policy")
@router.get("/acceptable-use-policy")
@router.get("/acceptable-use")
@router.get("/aup")
@limiter.exempt
async def acceptable_use_policy(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "legal/acceptable-use-policy.html",
        {"host_url": str(request.base_url)},
    )


@router.get("/docs/{path:path}")
@limiter.exempt
async def docs_wildcard(path: str, request: Request) -> Response:
    return RedirectResponse(f"{DOCS_ROOT}/{path}", status_code=301)


# ── Contact ──────────────────────────────────────────────────────────────────


@router.api_route("/contact", methods=["GET", "POST"], include_in_schema=False)
@limiter.limit(Limits.CONTACT)
async def contact(
    request: Request,
    contact_service: ContactSvc,
    settings: Settings,
) -> Response:
    host_url = str(request.base_url)

    if not settings.contact_webhook:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "error_code": "503",
                "error_message": "CONTACT FORM NOT CONFIGURED",
                "host_url": host_url,
            },
            status_code=503,
        )

    if request.method == "GET":
        return templates.TemplateResponse(
            request,
            "contact.html",
            {"host_url": host_url},
        )

    form = await request.form()
    email = form.get("email")
    message = form.get("message")
    captcha_token = form.get("h-captcha-response", "")

    if settings.hcaptcha_sitekey and not captcha_token:
        return templates.TemplateResponse(
            request,
            "contact.html",
            {
                "error": "Please complete the captcha",
                "host_url": host_url,
                "email": email,
                "message": message,
            },
            status_code=400,
        )

    if not email or not message:
        return templates.TemplateResponse(
            request,
            "contact.html",
            {"error": "All fields are required", "host_url": host_url},
            status_code=400,
        )

    try:
        await contact_service.send_contact_message(email, message, captcha_token)
    except (ForbiddenError, AppError) as exc:
        return templates.TemplateResponse(
            request,
            "contact.html",
            {
                "error": str(exc),
                "host_url": host_url,
                "email": email,
                "message": message,
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "contact.html",
        {"success": "Message sent successfully", "host_url": host_url},
    )


# ── Report ───────────────────────────────────────────────────────────────────


@router.api_route("/report", methods=["GET", "POST"], include_in_schema=False)
@limiter.limit(Limits.CONTACT)
async def report(
    request: Request,
    contact_service: ContactSvc,
    url_service: UrlSvc,
    settings: Settings,
) -> Response:
    host_url = str(request.base_url)

    if not settings.url_report_webhook:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "error_code": "503",
                "error_message": "REPORT FORM NOT CONFIGURED",
                "host_url": host_url,
            },
            status_code=503,
        )

    if request.method == "GET":
        return templates.TemplateResponse(
            request,
            "report.html",
            {"host_url": host_url},
        )

    form = await request.form()
    short_code = form.get("short_code")
    reason = form.get("reason")
    captcha_token = form.get("h-captcha-response", "")

    if settings.hcaptcha_sitekey and not captcha_token:
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "error": "Please complete the captcha",
                "host_url": host_url,
                "short_code": short_code,
                "reason": reason,
            },
            status_code=400,
        )

    if not short_code or not reason:
        return templates.TemplateResponse(
            request,
            "report.html",
            {"error": "All fields are required", "host_url": host_url},
            status_code=400,
        )

    short_code = short_code.split("/")[-1]
    url_exists = not await url_service.check_alias_available(short_code)

    try:
        await contact_service.send_report(
            short_code,
            reason,
            get_client_ip(request),
            host_url,
            captcha_token,
            url_exists,
        )
    except (ForbiddenError, ValidationError, AppError) as exc:
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "error": str(exc),
                "host_url": host_url,
                "short_code": short_code,
                "reason": reason,
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "report.html",
        {"success": "Report sent successfully", "host_url": host_url},
    )
