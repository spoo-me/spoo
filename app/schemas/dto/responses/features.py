"""Response DTOs for GET /api/v1/me/features."""

from app.schemas.dto.base import ResponseBase
from app.schemas.enums.feature_state import FeatureState


class FeaturesResponse(ResponseBase):
    """Per-feature availability for the authenticated account.

    Keys are the exposed feature names (``services.feature_flag_service.
    EXPOSED_FEATURES``); values tell the client what to render. Clients
    must treat unknown keys as informational and missing keys as HIDDEN,
    so the feature list can grow without breaking older frontends.
    """

    features: dict[str, FeatureState]
