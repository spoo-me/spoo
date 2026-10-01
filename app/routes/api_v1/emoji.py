"""GET /api/v1/emoji-set — the accepted emoji-alias catalogue.

A public, unauthenticated read of the emoji-alias acceptance policy: the
set a user may choose from (the picker's list), the auto-generation pool,
the version caps, and the grapheme limit. It is a static, build-time
derived constant, so clients (an emoji picker, a dice suggester) have one
source of truth instead of replicating the policy. Both sets flow from
``shared.emoji_policy`` so they can never drift from the validator that
accepts aliases.

Caching is a day of freshness with a week of stale-while-revalidate, plus
a content-derived ETag so a deploy that bumps the emoji set (new Unicode
version) yields a new ETag and revalidates to fresh data instead of
stranding clients on a stale list. Low-volume, interaction-gated read: the
browser HTTP cache and a client memo carry it, no edge or server cache.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from fastapi import APIRouter, Request, Response

from app.dependencies import Settings
from app.middleware.openapi import ERROR_RESPONSES, PUBLIC_SECURITY
from app.middleware.rate_limiter import Limits, limiter
from app.schemas.dto.responses.emoji import EmojiEntry, EmojiSetResponse
from app.shared.emoji_policy import (
    accepted_singletons,
    emoji_display_name,
    emoji_group,
    emoji_keywords,
    emoji_sort_key,
    generation_pool,
)

router = APIRouter(tags=["URL Shortening"])

# A day fresh, a week of serve-stale-while-revalidating in the background.
# Not `immutable`: the set changes on deploy, and the ETag below makes that
# revalidation return fresh data rather than stranding clients until TTL.
_CACHE_CONTROL = "public, max-age=86400, stale-while-revalidate=604800"


@lru_cache(maxsize=4)
def _build_set(
    accept_cap: float, generate_cap: float, max_graphemes: int
) -> tuple[EmojiSetResponse, str]:
    """Build the response payload and its ETag for one policy triple.

    The whole response is a pure function of the three caps — the accept
    version, the generate version, and the grapheme limit — so it is memoized
    per triple. Building the ~1170 entries and hashing them is ~3ms; every
    request after the first for a given triple is a dict lookup. Returns the
    payload paired with its content-derived ETag so the 304 path is free too.
    """
    gen = set(generation_pool(generate_cap))
    # Accepted set stays policy-derived (the source of truth); grouping only
    # sorts it into canonical Unicode order and annotates each entry. Sorting
    # a set never changes membership, and every accepted char is emitted even
    # if the grouping data lacks it (fallback group, sorted last).
    accepted = sorted(accepted_singletons(accept_cap), key=emoji_sort_key)
    emoji_list = []
    for char in accepted:
        keywords = emoji_keywords(char)
        emoji_list.append(
            EmojiEntry(
                c=char,
                n=emoji_display_name(char),
                g=emoji_group(char),
                gen=char in gen,
                k=list(keywords) or None,
            )
        )
    payload = EmojiSetResponse(
        accept_max_version=accept_cap,
        generate_max_version=generate_cap,
        max_graphemes=max_graphemes,
        emoji=emoji_list,
    )
    return payload, _etag(payload)


def _etag(payload: EmojiSetResponse) -> str:
    """A strong, quoted ETag derived from the set's content and caps.

    Any change to the caps, the emoji list, its ORDER, or an entry's
    name/group/gen/aliases (a deploy bumping the pinned ``emoji`` package or
    the grouping artifact, or a settings override) changes the digest, so the
    ETag revalidates to fresh data for free. Order is captured implicitly by
    hashing entries in array order.
    """
    h = hashlib.sha256()
    h.update(
        f"{payload.accept_max_version}|{payload.generate_max_version}"
        f"|{payload.max_graphemes}|".encode()
    )
    for entry in payload.emoji:
        h.update(entry.c.encode())
        h.update(b"1" if entry.gen else b"0")
        h.update(entry.n.encode())
        h.update(entry.g.encode())
        h.update(",".join(entry.k or ()).encode())
        h.update(b"\x00")
    return f'"{h.hexdigest()[:16]}"'


@router.get(
    "/emoji-set",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="getEmojiSet",
    summary="Accepted Emoji Set",
    response_model_exclude_none=True,
)
@limiter.limit(Limits.API_CHECK_ANON)
async def emoji_set(
    request: Request,
    response: Response,
    settings: Settings,
) -> EmojiSetResponse:
    """Return the accepted emoji catalogue and its policy caps.

    ``emoji`` lists every single-codepoint emoji a custom alias may use
    (the picker's list), ordered by canonical Unicode group and within-group
    order so a picker opens on Smileys rather than symbols. Each entry
    carries ``c`` (the raw canonical character), ``n`` (a searchable name like
    "rocket"), ``g`` (its canonical Unicode category, e.g. "Smileys &
    Emotion", for category tabs), ``gen`` (whether it is in the server's
    auto-generation pool), and an optional ``k`` (extra search aliases when
    the source lists any). Skin tone is a client-side modifier appended to a
    base emoji, so skin-tone variants are not enumerated.

    **Authentication**: None. The response is identical for everyone.

    **Caching**: Fresh for a day, then served stale for a week while
    revalidating. A content-derived ``ETag`` is returned; send it back as
    ``If-None-Match`` to get a ``304`` when the set is unchanged.

    **Rate Limits**: 60/min, 2,000/day.
    """
    payload, etag = _build_set(
        settings.emoji_accept_max_version,
        settings.emoji_generate_max_version,
        settings.max_emoji_alias_length,
    )

    # RFC 9110 §13.1.2: If-None-Match uses the weak comparison, so a strong
    # ETag matches its weak (``W/``-prefixed) form. Cloudflare weakens strong
    # ETags on compressed responses, so real clients echo back ``W/"..."``;
    # strip the prefix before comparing, and honor the ``*`` wildcard.
    if_none_match = request.headers.get("if-none-match", "")
    tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    if etag in tags or "*" in tags:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": _CACHE_CONTROL},
        )

    response.headers["Cache-Control"] = _CACHE_CONTROL
    response.headers["ETag"] = etag
    return payload
