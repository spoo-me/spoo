"""
GET    /api/v1/tags           — your tags with link counts
POST   /api/v1/tags           — create a tag
PATCH  /api/v1/tags/{tag_id}  — rename or recolour
DELETE /api/v1/tags/{tag_id}  — delete and strip from every link

Tags are the per-account registry links point at by id (``tag_ids`` on
create/patch, ``tagIds``/``tagNames`` on the list filter, ``tag_id``/``tag``
on stats). Free for everyone; the same scopes as link management.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from dependencies import (
    URL_MANAGEMENT_SCOPES,
    URL_READ_SCOPES,
    CurrentUser,
    Entitled,
    TagSvc,
    require_scopes,
)
from middleware.openapi import ERROR_RESPONSES
from middleware.rate_limiter import Limits, limiter, plan_scaled
from routes.api_v1._helpers import parse_object_id
from schemas.dto.requests.tag import CreateTagRequest, UpdateTagRequest
from schemas.dto.responses.tag import (
    TagDeleteResponse,
    TagListResponse,
    TagResponse,
)

router = APIRouter(tags=["Tags"])


_TAG_ID_PATH = Path(
    description="Tag id (MongoDB ObjectId), as returned by GET /tags.",
    examples=["665f0c2f9e7a4b1d2c3d4e5f"],
)


@router.get(
    "/tags",
    responses=ERROR_RESPONSES,
    operation_id="listTags",
    summary="List Your Tags",
)
@limiter.limit(plan_scaled(Limits.API_AUTHED))
async def list_tags_v1(
    request: Request,
    entitlements: Entitled,
    tag_service: TagSvc,
    user: CurrentUser = Depends(require_scopes(URL_READ_SCOPES)),  # noqa: B008
) -> TagListResponse:
    """Every tag in your account with the number of links carrying it,
    oldest first.

    **API Key Scope**: `urls:manage`, `urls:read`, or `admin:all`

    **Rate Limits**: 60/min, 5,000/day
    """
    rows = await tag_service.list_with_counts(user.user_id)
    return TagListResponse(items=[TagResponse.from_doc(doc, n) for doc, n in rows])


@router.post(
    "/tags",
    status_code=201,
    responses=ERROR_RESPONSES,
    operation_id="createTag",
    summary="Create a Tag",
)
@limiter.limit(Limits.TAG_WRITE)
async def create_tag_v1(
    request: Request,
    body: CreateTagRequest,
    tag_service: TagSvc,
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> TagResponse:
    """Create a tag. Names are lowercased and trimmed; a name you already
    have answers `409 conflict`. Omit `color` to get the least-used palette
    colour in your account; omit `icon` for the generic tag glyph. At most
    500 tags per account.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 30/min
    """
    doc = await tag_service.create(user.user_id, body.name, body.color, body.icon)
    return TagResponse.from_doc(doc, 0)


@router.patch(
    "/tags/{tag_id}",
    responses=ERROR_RESPONSES,
    operation_id="updateTag",
    summary="Rename or Recolour a Tag",
)
@limiter.limit(Limits.TAG_WRITE)
async def update_tag_v1(
    request: Request,
    body: UpdateTagRequest,
    tag_service: TagSvc,
    tag_id: Annotated[str, _TAG_ID_PATH],
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> TagResponse:
    """Change the name, colour or icon. Links keep pointing at the tag
    by id, so a rename shows up everywhere at once. Renaming onto a name you
    already have answers `409 conflict`.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 30/min
    """
    oid = parse_object_id(tag_id, message="Invalid tag id format")
    doc = await tag_service.update(
        user.user_id,
        oid,
        name=body.name,
        color=body.color,
        icon=body.icon,
    )
    return TagResponse.from_doc(doc, await tag_service.link_count(user.user_id, oid))


@router.delete(
    "/tags/{tag_id}",
    responses=ERROR_RESPONSES,
    operation_id="deleteTag",
    summary="Delete a Tag",
)
@limiter.limit(Limits.TAG_DELETE)
async def delete_tag_v1(
    request: Request,
    tag_service: TagSvc,
    tag_id: Annotated[str, _TAG_ID_PATH],
    user: CurrentUser = Depends(require_scopes(URL_MANAGEMENT_SCOPES)),  # noqa: B008
) -> TagDeleteResponse:
    """Delete the tag and remove it from every link that carried it. The
    links themselves are untouched otherwise.

    **API Key Scope**: `urls:manage` or `admin:all`

    **Rate Limits**: 10/min. Deleting fans out an update over every link
    you own, so it carries the same budget as bulk delete.
    """
    oid = parse_object_id(tag_id, message="Invalid tag id format")
    links_updated = await tag_service.delete(user.user_id, oid)
    return TagDeleteResponse(links_updated=links_updated)
