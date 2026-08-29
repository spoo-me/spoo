"""
Legacy URL shortening routes — preserved for backwards compatibility.

GET  /              → index page (HTML)
POST /              → legacy v1 shorten (content-negotiated: JSON or redirect)
GET  /emoji         → emoji page (HTML)
POST /emoji         → emoji URL creation
GET  /result/<code> → result page (HTML)
GET  /<code>+       → preview page (HTML)
GET  /metric        → global metrics (JSON, DualCache)
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from dependencies import OptionalUser, Settings, UrlPolicy, UrlSvc, get_db, get_redis
from errors import ForbiddenError, GoneError, NotFoundError
from infrastructure.cache.dual_cache import DualCache
from infrastructure.logging import get_logger
from infrastructure.templates import templates
from middleware.rate_limiter import Limits, limiter
from repositories.legacy.emoji_url_repository import EmojiUrlRepository
from repositories.legacy.legacy_url_repository import LegacyUrlRepository
from repositories.url_repository import UrlRepository
from routes.legacy.helpers import humanize_number, is_positive_integer
from schemas.models.url import UrlStatus
from shared.emoji_policy import canonicalize_emoji_alias, check_emoji_alias
from shared.generators import generate_emoji_alias, generate_short_code
from shared.url_utils import parse_destination, split_destination
from shared.validators import (
    is_emoji_alias,
    validate_alias,
    validate_safe_redirect,
    validate_url_password,
)

log = get_logger(__name__)

router = APIRouter(include_in_schema=False)

METRIC_PIPELINE_V1 = [
    {
        "$group": {
            "_id": None,
            "total-shortlinks": {"$sum": 1},
            "total-clicks": {"$sum": "$total-clicks"},
        }
    }
]


# ── Index ─────────────────────────────────────────────────────────────────────


@router.get("/")
@limiter.exempt
async def index(request: Request, user: OptionalUser) -> Response:
    """Render the index page. Redirect to dashboard if already logged in."""
    if user is not None:
        next_url = validate_safe_redirect(request.query_params.get("next", ""))
        return RedirectResponse(next_url, status_code=302)
    return templates.TemplateResponse(
        request, "index.html", {"host_url": str(request.base_url)}
    )


# ── Legacy v1 URL shortening ──────────────────────────────────────────────────


@router.post("/")
@limiter.limit(Limits.SHORTEN_LEGACY)
async def shorten_url(
    request: Request,
    url_service: UrlSvc,
    url_policy: UrlPolicy,
    settings: Settings,
    db=Depends(get_db),
) -> Response:
    """Legacy v1 URL shortening.

    Accepts form data or query params. Response format is determined by the
    ``Accept: application/json`` header — returns JSON or redirects to /result/.
    """
    form = await request.form()
    wants_json = request.headers.get("Accept") == "application/json"
    host_url = str(request.base_url)

    def _get(key: str) -> str | None:
        return form.get(key) or request.query_params.get(key) or None

    url = _get("url")
    password = _get("password")
    max_clicks = _get("max-clicks")
    alias = _get("alias")
    block_bots = _get("block-bots")

    if not url:
        if wants_json:
            return JSONResponse({"UrlError": "URL is required"}, status_code=400)
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": "URL is required", "host_url": host_url},
            status_code=400,
        )

    # The shared L0 gate (format + self-link + patterns + threat feeds) —
    # same instance the v2 create path uses. Frozen legacy wire shapes.
    rejection = await url_policy.check(url)
    if rejection is not None:
        log.info("url_creation_failed", reason=rejection.code, schema="v1")
        if rejection.code == "invalid_url":
            return JSONResponse(
                {
                    "UrlError": (
                        "Invalid URL, URL must have a valid protocol and must follow"
                        " rfc_1034 & rfc_2728 patterns"
                    )
                },
                status_code=400,
            )
        # Published policies get their message; security stays coarse. Frozen key.
        return JSONResponse(
            {"BlockedUrlError": rejection.public_message}, status_code=403
        )

    if alias and not validate_alias(alias):
        if wants_json:
            return JSONResponse(
                {"AliasError": "Invalid Alias", "alias": alias}, status_code=400
            )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": "Invalid Alias",
                "url": url,
                "host_url": host_url,
            },
            status_code=400,
        )

    if alias:
        alias = alias[:16]
        if not await url_service.check_alias_available(alias):
            log.warning(
                "url_creation_failed", reason="alias_exists", alias=alias, schema="v1"
            )
            if wants_json:
                return JSONResponse(
                    {"AliasError": "Alias already exists", "alias": alias},
                    status_code=400,
                )
            return templates.TemplateResponse(
                request,
                "index.html",
                {
                    "error": f"Alias {alias} already exists",
                    "url": url,
                    "host_url": host_url,
                },
                status_code=400,
            )
        short_code = alias
    else:
        legacy_repo = LegacyUrlRepository(db["urls"])
        url_repo = UrlRepository(db["urlsV2"])
        for _ in range(20):
            candidate = generate_short_code()
            if not await legacy_repo.check_exists(
                candidate
            ) and not await url_repo.check_alias_exists(
                candidate, settings.system_default_domain
            ):
                short_code = candidate
                break
        else:
            return JSONResponse(
                {"UrlError": "Could not generate unique alias"}, status_code=500
            )

    data: dict = {
        "url": url,
        "counter": {},
        "total-clicks": 0,
        "ips": [],
        "creation-date": datetime.now().strftime("%Y-%m-%d"),
        "creation-time": datetime.now().strftime("%H:%M:%S"),
        "creation-ip-address": request.client.host if request.client else "unknown",
    }
    # Same parsed-destination subdoc as v2 creates, so safety sweeps and
    # takedown pivots cover the legacy collections via the sparse index.
    if dest_parts := parse_destination(url):
        data["dest"] = dest_parts

    if password:
        if not validate_url_password(
            password, min_length=settings.url_password_min_length
        ):
            return JSONResponse(
                {
                    "PasswordError": (
                        f"Invalid password, password must be at least {settings.url_password_min_length} characters long,"
                        " must contain a letter and a number and a special character"
                        " either '@' or '.' and cannot be consecutive"
                    )
                },
                status_code=400,
            )
        data["password"] = password

    if max_clicks:
        if not is_positive_integer(max_clicks):
            return JSONResponse(
                {"MaxClicksError": "max-clicks must be an positive integer"},
                status_code=400,
            )
        data["max-clicks"] = str(abs(int(max_clicks)))

    if block_bots:
        data["block-bots"] = True

    legacy_repo = LegacyUrlRepository(db["urls"])
    await legacy_repo.insert(short_code, data)
    await url_policy.record_create(url)

    log.info(
        "url_created",
        alias=short_code,
        long_url=url,
        schema="v1",
        has_password=bool(password),
        max_clicks=max_clicks or None,
        block_bots=bool(block_bots),
    )

    response_data = {
        "short_url": f"{host_url}{short_code}",
        "domain": request.url.hostname,
        "original_url": url,
    }

    if wants_json:
        return JSONResponse(response_data)
    return RedirectResponse(f"/result/{short_code}", status_code=302)


# ── Emoji URL shortening ──────────────────────────────────────────────────────


@router.api_route("/emoji", methods=["GET", "POST"], include_in_schema=False)
@limiter.limit(Limits.SHORTEN_LEGACY)
async def emoji(
    request: Request,
    url_service: UrlSvc,
    url_policy: UrlPolicy,
    settings: Settings,
    db=Depends(get_db),
) -> Response:
    """Emoji URL shortening — reads form/query params, validates, and creates emoji URL.

    Matches Flask behavior: both GET and POST fall through to the same validation
    (no separate emoji page template exists). Wire contract is frozen
    (``emojies`` param, ``EmojiError`` shapes, raw-emoji ``short_url``), but
    NEW creations are held to the v2 emoji policy (``shared.emoji_policy``)
    and stored canonical — v1's accept-any-emoji behavior minted byte-fragile
    codes that broke in browsers. Existing links resolve unchanged.
    """
    if request.method == "POST":
        form = await request.form()
    else:
        form = {}

    def _get(key: str) -> str | None:
        return form.get(key) or request.query_params.get(key) or None

    emojies = _get("emojies")
    url = _get("url")
    password = _get("password")
    max_clicks = _get("max-clicks")
    block_bots = _get("block-bots")

    if not url:
        return JSONResponse({"UrlError": "URL is required"}, status_code=400)

    emoji_repo = EmojiUrlRepository(db["emojis"])

    if emojies:
        emojies = canonicalize_emoji_alias(emojies)
        verdict = check_emoji_alias(
            emojies,
            max_graphemes=settings.max_emoji_alias_length,
            max_version=settings.emoji_accept_max_version,
        )
        if verdict != "ok":
            return JSONResponse({"EmojiError": "Invalid emoji"}, status_code=400)
        # Cross-system uniqueness: urlsV2 (canonical) + legacy emojis
        # (VS16-insensitive) — a new legacy doc must not be shadowed by the
        # v2-first resolve order, and vice versa.
        if not await url_service.check_alias_available(emojies):
            log.warning(
                "url_creation_failed", reason="emoji_alias_exists", alias=emojies
            )
            return JSONResponse({"EmojiError": "Emoji already exists"}, status_code=400)
    else:
        for _ in range(20):
            candidate = generate_emoji_alias()
            if await url_service.check_alias_available(candidate):
                emojies = candidate
                break
        else:
            return JSONResponse(
                {"EmojiError": "Could not generate unique emoji alias"}, status_code=500
            )

    # Shared L0 gate; note this route's frozen blocked shape is UrlError,
    # not BlockedUrlError. (Also fixes the old bug where this path dropped
    # the configured regex timeout.)
    rejection = await url_policy.check(url)
    if rejection is not None:
        log.info("url_creation_failed", reason=rejection.code, schema="emoji")
        if rejection.code == "invalid_url":
            return JSONResponse(
                {
                    "UrlError": (
                        "Invalid URL, URL must have a valid protocol and must follow"
                        " rfc_1034 & rfc_2728 patterns"
                    )
                },
                status_code=400,
            )
        return JSONResponse({"UrlError": rejection.public_message}, status_code=403)

    data: dict = {
        "url": url,
        "counter": {},
        "total-clicks": 0,
        "ips": [],
        "creation-date": datetime.now().strftime("%Y-%m-%d"),
        "creation-time": datetime.now().strftime("%H:%M:%S"),
        "creation-ip-address": request.client.host if request.client else "unknown",
    }
    # Same parsed-destination subdoc as v2 creates, so safety sweeps and
    # takedown pivots cover the legacy collections via the sparse index.
    if dest_parts := parse_destination(url):
        data["dest"] = dest_parts

    if password:
        if not validate_url_password(
            password, min_length=settings.url_password_min_length
        ):
            return JSONResponse(
                {
                    "PasswordError": (
                        f"Invalid password, password must be at least {settings.url_password_min_length} characters long,"
                        " must contain a letter and a number and a special character"
                        " either '@' or '.' and cannot be consecutive"
                    )
                },
                status_code=400,
            )
        data["password"] = password

    if max_clicks:
        if not is_positive_integer(max_clicks):
            return JSONResponse(
                {"MaxClicksError": "max-clicks must be an positive integer"},
                status_code=400,
            )
        data["max-clicks"] = str(abs(int(max_clicks)))

    if block_bots:
        data["block-bots"] = True

    await emoji_repo.insert(emojies, data)
    await url_policy.record_create(url)

    log.info(
        "url_created",
        alias=emojies,
        long_url=url,
        schema="v1_emoji",
        has_password=bool(password),
    )

    response_data = {
        "short_url": f"{request.base_url}{emojies}",
        "domain": request.url.hostname,
        "original_url": url,
    }

    if request.headers.get("Accept") == "application/json":
        return JSONResponse(response_data)
    return RedirectResponse(f"/result/{emojies}", status_code=302)


# ── Result page ───────────────────────────────────────────────────────────────


@router.get("/result/{short_code}")
@limiter.exempt
async def result(
    short_code: str,
    request: Request,
    url_service: UrlSvc,
) -> Response:
    """Show the result page after a URL is shortened."""
    short_code = unquote(short_code)
    host_url = str(request.base_url)

    try:
        url_data, _ = await url_service.resolve(short_code)
        short_url = f"{host_url}{url_data.alias}"
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "short_url": short_url,
                "short_code": url_data.alias,
                "host_url": host_url,
            },
        )
    except (NotFoundError, ForbiddenError, GoneError):
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "error_code": "404",
                "error_message": "URL NOT FOUND",
                "host_url": host_url,
            },
            status_code=404,
        )


# ── Preview page ──────────────────────────────────────────────────────────────


@router.get("/{short_code}+")
@limiter.limit(Limits.PUBLIC_PREVIEW)
async def preview_url(
    short_code: str,
    request: Request,
    settings: Settings,
    db=Depends(get_db),
) -> Response:
    """Show a preview of where a short URL redirects to.

    Accesses repos directly so an EXPIRED link still previews, but a
    blocked one reveals nothing: destination, geo destinations and meta
    tags are all withheld behind the same 451 the redirect serves.
    """
    short_code = unquote(short_code)
    host_url = str(request.base_url)

    # Dispatch by type — mirrors get_url_by_length_and_type heuristic
    url_data = None
    schema_type = "v1"

    if is_emoji_alias(short_code):
        emoji_repo = EmojiUrlRepository(db["emojis"])
        doc = await emoji_repo.find_by_id(short_code)
        if doc:
            url_data = {
                "_id": short_code,
                "url": doc.url,
                "password": doc.password,
                "blocked": doc.effective_status is UrlStatus.BLOCKED,
            }
            schema_type = "emoji"
    else:
        url_repo = UrlRepository(db["urlsV2"])
        legacy_repo = LegacyUrlRepository(db["urls"])
        code_len = len(short_code)

        if code_len == 6:
            # v1 first
            doc = await legacy_repo.find_by_id(short_code)
            if doc:
                url_data = {
                    "_id": short_code,
                    "url": doc.url,
                    "password": doc.password,
                    "blocked": doc.effective_status is UrlStatus.BLOCKED,
                }
                schema_type = "v1"
            else:
                v2 = await url_repo.find_by_alias(
                    short_code, settings.system_default_domain
                )
                if v2:
                    url_data = {
                        "alias": v2.alias,
                        "long_url": v2.long_url,
                        "password": v2.password,
                        "geo_rules": v2.geo_rules,
                        "meta_tags": v2.meta_tags.model_dump()
                        if v2.meta_tags
                        else None,
                        "blocked": v2.effective_status is UrlStatus.BLOCKED,
                    }
                    schema_type = "v2"
        else:
            # v2 first
            v2 = await url_repo.find_by_alias(
                short_code, settings.system_default_domain
            )
            if v2:
                url_data = {
                    "alias": v2.alias,
                    "long_url": v2.long_url,
                    "password": v2.password,
                    "geo_rules": v2.geo_rules,
                    "meta_tags": v2.meta_tags.model_dump() if v2.meta_tags else None,
                    "blocked": v2.effective_status is UrlStatus.BLOCKED,
                }
                schema_type = "v2"
            else:
                doc = await legacy_repo.find_by_id(short_code)
                if doc:
                    url_data = {
                        "_id": short_code,
                        "url": doc.url,
                        "password": doc.password,
                        "blocked": doc.effective_status is UrlStatus.BLOCKED,
                    }
                    schema_type = "v1"

    if not url_data:
        return templates.TemplateResponse(
            request,
            "preview.html",
            {
                "error": "URL not found",
                "short_code": short_code,
                "host_url": host_url,
            },
            status_code=404,
        )

    if url_data.get("blocked"):
        log.info("legacy_preview_blocked", short_code=short_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "error_code": "451",
                "error_message": "This link has been disabled",
                "host_url": host_url,
            },
            status_code=451,
        )

    if schema_type == "v2":
        alias = url_data["alias"]
        long_url = url_data["long_url"]
    else:
        alias = url_data["_id"]
        long_url = url_data["url"]
    has_password = bool(url_data.get("password"))

    if has_password:
        return templates.TemplateResponse(
            request,
            "preview.html",
            {
                "alias": alias,
                "short_url": f"{host_url}{alias}",
                "password_protected": True,
                "host_url": host_url,
            },
        )

    default_dest = split_destination(long_url)

    # Geo-targeted links list EVERY destination — the preview page is the
    # anti-cloaking transparency surface, so no rule is ever hidden. Grouped
    # by destination URL (display only; storage stays a flat code→url map).
    geo_destinations = None
    if url_data.get("geo_rules"):
        grouped: dict[str, list[str]] = {}
        for code, dest in url_data["geo_rules"].items():
            grouped.setdefault(dest, []).append(code)
        geo_destinations = [
            {"countries": sorted(codes), **split_destination(dest)}
            for dest, codes in grouped.items()
        ]

    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "alias": alias,
            "short_url": f"{host_url}{alias}",
            "long_url": long_url,
            "domain": default_dest["domain"],
            "path": default_dest["path"],
            "is_https": default_dest["is_https"],
            "geo_destinations": geo_destinations,
            "password_protected": False,
            "host_url": host_url,
            # Anti-phishing transparency: show the custom card NEXT TO the
            # real destination — what the sender wants you to see vs where
            # you'll actually go. v2-only field; None for v1/emoji docs.
            "meta_tags": url_data.get("meta_tags") if schema_type == "v2" else None,
        },
    )


# ── Global metrics ────────────────────────────────────────────────────────────


@router.get("/metric")
@limiter.exempt
async def metric(
    request: Request,
    settings: Settings,
    db=Depends(get_db),
    redis=Depends(get_redis),
) -> Response:
    """Return global platform metrics, cached for 24 hours via DualCache."""
    dual_cache = DualCache(redis)
    http_client = request.app.state.http_client
    github_repo = settings.github_repo

    async def query() -> dict:
        start = time.time()

        cursor = await db["urls"].aggregate(METRIC_PIPELINE_V1)
        results = await cursor.to_list(length=1)
        v1_result = results[0] if results else {}
        v1_shortlinks = v1_result.get("total-shortlinks", 0)
        v1_clicks = v1_result.get("total-clicks", 0)

        v2_shortlinks = await db["urlsV2"].estimated_document_count()
        total_clicks_ts = await db["clicks"].estimated_document_count()

        total_shortlinks = v1_shortlinks + v2_shortlinks
        total_clicks = v1_clicks + total_clicks_ts

        github_stars = 0
        try:
            resp = await http_client.get(
                f"https://api.github.com/repos/{github_repo}", timeout=5
            )
            if resp.status_code == 200:
                github_stars = resp.json().get("stargazers_count", 0)
        except Exception as exc:
            log.warning("github_stars_fetch_failed", error=str(exc))

        elapsed = time.time() - start
        log.info(
            "metrics_query_completed",
            total_shortlinks=total_shortlinks,
            total_clicks=total_clicks,
            elapsed_ms=round(elapsed * 1000, 2),
        )
        return {
            "total-shortlinks-raw": total_shortlinks,
            "total-clicks-raw": total_clicks,
            "total-shortlinks": humanize_number(total_shortlinks),
            "total-clicks": humanize_number(total_clicks),
            "github-stars": github_stars,
        }

    result = await dual_cache.get_or_set(
        "metrics", query, primary_ttl=86400, stale_ttl=90000
    )
    if result is None:
        return Response(status_code=204)
    return JSONResponse(result)
