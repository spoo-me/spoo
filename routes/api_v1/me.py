"""
DELETE /api/v1/me                — request account deletion (grace period)
GET    /api/v1/me/entitlements   — plan, feature states, limits, version
GET    /api/v1/me/features       — the features map alone
GET    /api/v1/me/layouts/{page} — fetch the saved dashboard layout (null = default)
PUT    /api/v1/me/layouts/{page} — save the layout document verbatim
DELETE /api/v1/me/layouts/{page} — reset to default (idempotent)
GET    /api/v1/me/profile-pictures        — available pictures
POST   /api/v1/me/profile-pictures        — set a provider picture by id
POST   /api/v1/me/profile-pictures/upload — upload a custom picture (data URI)
DELETE /api/v1/me/profile-pictures        — unset the picture

Per-user preferences namespace. Layout documents are client-owned JSON blobs:
the frontend versions and validates them, the server stores them opaquely
keyed by (user, page). Features mirror the flag service's answers as data —
the read side of gates the write endpoints already enforce.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request

from dependencies import (
    AccountDeletionSvc,
    Entitled,
    EntitlementSvc,
    FeatureFlagSvc,
    JwtUser,
    PageLayoutSvc,
    ProfilePictureSvc,
)
from middleware.openapi import AUTH_RESPONSES
from middleware.rate_limiter import Limits, limiter
from schemas.dto.requests.account import DeleteAccountRequest
from schemas.dto.requests.layouts import PutLayoutRequest
from schemas.dto.requests.profile_pictures import (
    SetProfilePictureRequest,
    UploadProfilePictureRequest,
)
from schemas.dto.responses.account import AccountDeletionResponse
from schemas.dto.responses.entitlements import (
    EntitlementsResponse,
    LimitBlock,
    PlanBlock,
)
from schemas.dto.responses.features import FeaturesResponse
from schemas.dto.responses.layouts import LayoutResponse
from schemas.dto.responses.profile_pictures import (
    AvailablePicturesResponse,
    ProfilePictureMessageResponse,
)
from services.features.catalog import int_features

router = APIRouter(prefix="/me", tags=["Me"])


@router.delete(
    "",
    responses=AUTH_RESPONSES,
    operation_id="deleteMyAccount",
    summary="Delete Account",
)
# Tightest account-security budget in the file (ties RESEND_VERIFICATION's
# hourly cap): the re-auth body makes this endpoint a password oracle.
@limiter.limit(Limits.PASSWORD_RESET_REQUEST)
async def delete_my_account(
    request: Request,
    body: DeleteAccountRequest,
    user: JwtUser,
    deletion_service: AccountDeletionSvc,
) -> AccountDeletionResponse:
    """Request permanent account deletion (GDPR Art. 17).

    Re-authentication is required: accounts with a password send
    ``password``; OAuth-only accounts confirm by typing their exact
    account email as ``confirm_email``. On success the account enters a
    grace period (7 days by default) and `purge_after` marks its end —
    after that instant a background sweep permanently erases the account,
    its links, and their analytics.

    During the grace period every login is blocked with error code
    ``ACCOUNT_PENDING_DELETION``; ``POST /auth/restore`` cancels the
    deletion and reactivates the account — with email + password, or with
    the one-shot link mailed on this request (the OAuth-only path).

    **Authentication**: Required (JWT only — API keys and app tokens
    cannot delete the account)

    **Rate Limits**: 3/hour

    **Errors**: 403 when re-authentication fails (never says which field
    was wrong), 409 when deletion is already pending.
    """
    purge_after = await deletion_service.request_deletion(
        user.user_id,
        password=body.password,
        confirm_email=body.confirm_email,
    )
    return AccountDeletionResponse(purge_after=purge_after)


@router.get(
    "/features",
    responses=AUTH_RESPONSES,
    operation_id="getMyFeatures",
    summary="Get Feature Availability",
)
@limiter.limit(Limits.DASHBOARD_READ)
async def get_my_features(
    request: Request,
    user: JwtUser,
    flag_service: FeatureFlagSvc,
    entitlements: Entitled,
) -> FeaturesResponse:
    """Return the availability state of every gated feature for this account.

    States: `enabled` (render it), `hidden` (the feature doesn't exist for
    this account), `locked` (render it upgrade-gated: the plan does not
    include it). Treat features missing from the map as `hidden`. Never used
    for enforcement — the write endpoints enforce the same gates server-side.

    **Authentication**: Required.
    """
    return FeaturesResponse(features=await flag_service.states_for(user, entitlements))


@router.get(
    "/entitlements",
    responses=AUTH_RESPONSES,
    operation_id="getMyEntitlements",
    summary="My Entitlements",
)
@limiter.limit(Limits.DASHBOARD_READ)
async def get_my_entitlements(
    request: Request,
    user: JwtUser,
    flag_service: FeatureFlagSvc,
    entitlements: Entitled,
    entitlement_service: EntitlementSvc,
) -> EntitlementsResponse:
    """Return the account's plan, feature states, limits with live usage, and
    the entitlement version.

    `version` changes on every subscription or override write. After a
    checkout, poll until it changes; on every authenticated response,
    compare it with the `X-Entitlements-Version` header and refetch when
    they differ.

    **Authentication**: Required.
    """
    features = await flag_service.states_for(user, entitlements)
    used = await entitlement_service.usage_for(user.user_id)
    return EntitlementsResponse(
        version=entitlements.version,
        plan=PlanBlock(
            name=entitlements.plan.value,
            status=entitlements.status.value if entitlements.status else None,
            until=entitlements.until,
            founding=entitlements.founding,
        ),
        features=features,
        limits={
            f.key: LimitBlock(max=entitlements.limit(f.key), used=used.get(f.key))
            for f in int_features()
        },
    )


# Closed set: every dashboard board the frontend actually renders. An
# allowlist (not just a pattern) caps per-user storage and rejects junk
# slugs. Grows in lockstep with the frontend's boards.
PagePath = Annotated[
    Literal["analytics", "overview"],
    Path(description="Layout slot, e.g. `analytics`"),
]


@router.get(
    "/layouts/{page}",
    responses=AUTH_RESPONSES,
    operation_id="getPageLayout",
    summary="Get Page Layout",
)
@limiter.limit(Limits.LAYOUT_READ)
async def get_page_layout(
    request: Request,
    page: PagePath,
    user: JwtUser,
    layout_service: PageLayoutSvc,
) -> LayoutResponse:
    """Fetch the saved dashboard layout for a page.

    Returns `layout: null` when the user has never customized this page —
    clients render their built-in default in that case.

    **Authentication**: Required.
    """
    return LayoutResponse(layout=await layout_service.get_layout(user.user_id, page))


@router.put(
    "/layouts/{page}",
    responses=AUTH_RESPONSES,
    operation_id="putPageLayout",
    summary="Save Page Layout",
)
@limiter.limit(Limits.LAYOUT_WRITE)
async def put_page_layout(
    request: Request,
    page: PagePath,
    body: PutLayoutRequest,
    user: JwtUser,
    layout_service: PageLayoutSvc,
) -> LayoutResponse:
    """Save the layout document for a page.

    The document is stored verbatim (last write wins) and echoed back.
    Versioning and validation are the client's responsibility; the body is
    capped at 32 KiB.

    **Authentication**: Required.
    """
    return LayoutResponse(
        layout=await layout_service.put_layout(user.user_id, page, body.layout)
    )


@router.delete(
    "/layouts/{page}",
    status_code=204,
    responses=AUTH_RESPONSES,
    operation_id="deletePageLayout",
    summary="Reset Page Layout",
)
@limiter.limit(Limits.LAYOUT_DELETE)
async def delete_page_layout(
    request: Request,
    page: PagePath,
    user: JwtUser,
    layout_service: PageLayoutSvc,
) -> None:
    """Remove the saved layout so the page falls back to the client default.

    Idempotent: returns 204 whether or not an override existed.

    **Authentication**: Required.
    """
    await layout_service.delete_layout(user.user_id, page)


@router.get(
    "/profile-pictures",
    responses=AUTH_RESPONSES,
    operation_id="getMyProfilePictures",
    summary="Get Available Profile Pictures",
)
@limiter.limit(Limits.DASHBOARD_READ)
async def get_profile_pictures(
    request: Request,
    user: JwtUser,
    svc: ProfilePictureSvc,
) -> AvailablePicturesResponse:
    """List the profile pictures available to this account.

    One entry per linked OAuth provider picture, with `is_current` marking
    the active one.

    **Authentication**: Required.
    """
    pictures = await svc.get_available_pictures(user.user_id)
    return AvailablePicturesResponse(pictures=pictures)


@router.post(
    "/profile-pictures",
    responses=AUTH_RESPONSES,
    operation_id="setMyProfilePicture",
    summary="Set Profile Picture",
)
@limiter.limit(Limits.PROFILE_PICTURE_SET)
async def set_profile_picture(
    request: Request,
    body: SetProfilePictureRequest,
    user: JwtUser,
    svc: ProfilePictureSvc,
) -> ProfilePictureMessageResponse:
    """Set the profile picture to one of the available provider pictures.

    `picture_id` must be an id returned by the GET endpoint; unknown ids
    yield 404.

    **Authentication**: Required.
    """
    await svc.set_picture(user.user_id, body.picture_id)
    return ProfilePictureMessageResponse(message="Profile picture updated successfully")


@router.post(
    "/profile-pictures/upload",
    responses=AUTH_RESPONSES,
    operation_id="uploadMyProfilePicture",
    summary="Upload Profile Picture",
)
@limiter.limit(Limits.PROFILE_PICTURE_UPLOAD)
async def upload_profile_picture(
    request: Request,
    body: UploadProfilePictureRequest,
    user: JwtUser,
    svc: ProfilePictureSvc,
) -> ProfilePictureMessageResponse:
    """Upload a custom profile picture as a base64 data URI.

    Accepts image/png, image/jpeg and image/webp; size and content-type
    validation happens server-side against the configured upload cap.

    **Authentication**: Required.
    """
    await svc.upload_picture(user.user_id, body.image)
    return ProfilePictureMessageResponse(message="Profile picture updated successfully")


@router.delete(
    "/profile-pictures",
    responses=AUTH_RESPONSES,
    operation_id="deleteMyProfilePicture",
    summary="Remove Profile Picture",
)
@limiter.limit(Limits.PROFILE_PICTURE_SET)
async def unset_profile_picture(
    request: Request,
    user: JwtUser,
    svc: ProfilePictureSvc,
) -> ProfilePictureMessageResponse:
    """Unset the profile picture so the account falls back to the default.

    Idempotent: returns 200 whether or not a picture was set.

    **Authentication**: Required.
    """
    await svc.unset_picture(user.user_id)
    return ProfilePictureMessageResponse(message="Profile picture removed")
