"""
Service dependency providers.

Each function is a thin lookup that returns the singleton service instance
built during application startup in the lifespan (app.py).  No per-request
object construction — services are stateless and shared across requests.
"""

from __future__ import annotations

from typing import Annotated

from bson import ObjectId
from fastapi import Depends, Request

from errors import NotFoundError
from repositories.app_grant_repository import AppGrantRepository
from repositories.user_repository import UserRepository
from schemas.models.user import UserDoc
from services.api_key_service import ApiKeyService
from services.auth.credentials import CredentialService
from services.auth.device import DeviceAuthService
from services.auth.password import PasswordService
from services.auth.verification import EmailVerificationService
from services.bulk_url_service import BulkUrlService
from services.click import ClickService
from services.click.sinks import ClickEventSink
from services.contact_service import ContactService
from services.custom_domain_service import CustomDomainService
from services.domain_intel_service import DomainIntelService
from services.export.service import ExportService
from services.feature_flag_service import FeatureFlagService
from services.oauth_service import OAuthService
from services.page_layout_service import PageLayoutService
from services.profile_picture_service import ProfilePictureService
from services.public_preview_service import PublicPreviewService
from services.public_stats_service import PublicStatsService
from services.report_intake_service import ReportIntakeService
from services.stats_service import StatsService
from services.url_expand_service import UrlExpandService
from services.url_service import UrlService
from services.webhooks.service import WebhookService


def get_url_service(request: Request) -> UrlService:
    return request.app.state.url_service


def get_bulk_url_service(request: Request) -> BulkUrlService:
    return request.app.state.bulk_url_service


def get_stats_service(request: Request) -> StatsService:
    return request.app.state.stats_service


def get_public_stats_service(request: Request) -> PublicStatsService:
    return request.app.state.public_stats_service


def get_export_service(request: Request) -> ExportService:
    return request.app.state.export_service


def get_api_key_service(request: Request) -> ApiKeyService:
    return request.app.state.api_key_service


def get_page_layout_service(request: Request) -> PageLayoutService:
    return request.app.state.page_layout_service


def get_credential_service(request: Request) -> CredentialService:
    return request.app.state.credential_service


def get_verification_service(request: Request) -> EmailVerificationService:
    return request.app.state.verification_service


def get_password_service(request: Request) -> PasswordService:
    return request.app.state.password_service


def get_device_auth_service(request: Request) -> DeviceAuthService:
    return request.app.state.device_auth_service


def get_user_repo(request: Request) -> UserRepository:
    return request.app.state.user_repo


async def fetch_user_profile(user_repo: UserRepository, user_id: ObjectId) -> UserDoc:
    """Fetch a user by ID or raise NotFoundError.

    Thin helper used by route handlers that need a user profile
    without depending on a full auth service.
    """
    user = await user_repo.find_by_id(user_id)
    if not user:
        raise NotFoundError("user not found")
    return user


def get_oauth_service(request: Request) -> OAuthService:
    return request.app.state.oauth_service


def get_profile_picture_service(request: Request) -> ProfilePictureService:
    return request.app.state.profile_picture_service


def get_contact_service(request: Request) -> ContactService:
    return request.app.state.contact_service


def get_click_service(request: Request) -> ClickService:
    return request.app.state.click_service


def get_click_sink(request: Request) -> ClickEventSink:
    return request.app.state.click_sink


def get_app_grant_repo(request: Request) -> AppGrantRepository:
    return request.app.state.app_grant_repo


def get_feature_flag_service(request: Request) -> FeatureFlagService:
    return request.app.state.feature_flag_service


def get_custom_domain_service(request: Request) -> CustomDomainService:
    return request.app.state.custom_domain_service


def get_public_preview_service(request: Request) -> PublicPreviewService:
    return request.app.state.public_preview_service


def get_url_expand_service(request: Request) -> UrlExpandService:
    return request.app.state.url_expand_service


def get_domain_intel_service(request: Request) -> DomainIntelService:
    return request.app.state.domain_intel_service


def get_report_intake_service(request: Request) -> ReportIntakeService:
    return request.app.state.report_intake_service


def get_webhook_service(request: Request) -> WebhookService:
    return request.app.state.webhook_service


# ── Annotated type aliases — Depends shortcuts for route signatures ──────────

UrlSvc = Annotated[UrlService, Depends(get_url_service)]
BulkUrlSvc = Annotated[BulkUrlService, Depends(get_bulk_url_service)]
StatsSvc = Annotated[StatsService, Depends(get_stats_service)]
PublicStatsSvc = Annotated[PublicStatsService, Depends(get_public_stats_service)]
ExportSvc = Annotated[ExportService, Depends(get_export_service)]
ApiKeySvc = Annotated[ApiKeyService, Depends(get_api_key_service)]
PageLayoutSvc = Annotated[PageLayoutService, Depends(get_page_layout_service)]
CredentialSvc = Annotated[CredentialService, Depends(get_credential_service)]
VerificationSvc = Annotated[EmailVerificationService, Depends(get_verification_service)]
PasswordSvc = Annotated[PasswordService, Depends(get_password_service)]
DeviceAuthSvc = Annotated[DeviceAuthService, Depends(get_device_auth_service)]
UserRepo = Annotated[UserRepository, Depends(get_user_repo)]
OAuthSvc = Annotated[OAuthService, Depends(get_oauth_service)]
ProfilePictureSvc = Annotated[
    ProfilePictureService, Depends(get_profile_picture_service)
]
ContactSvc = Annotated[ContactService, Depends(get_contact_service)]
ClickSvc = Annotated[ClickService, Depends(get_click_service)]
ClickSink = Annotated[ClickEventSink, Depends(get_click_sink)]
AppGrantRepo = Annotated[AppGrantRepository, Depends(get_app_grant_repo)]
FeatureFlagSvc = Annotated[FeatureFlagService, Depends(get_feature_flag_service)]
CustomDomainSvc = Annotated[CustomDomainService, Depends(get_custom_domain_service)]
PublicPreviewSvc = Annotated[PublicPreviewService, Depends(get_public_preview_service)]
UrlExpandSvc = Annotated[UrlExpandService, Depends(get_url_expand_service)]
DomainIntelSvc = Annotated[DomainIntelService, Depends(get_domain_intel_service)]
ReportIntakeSvc = Annotated[ReportIntakeService, Depends(get_report_intake_service)]
WebhookSvc = Annotated[WebhookService, Depends(get_webhook_service)]
