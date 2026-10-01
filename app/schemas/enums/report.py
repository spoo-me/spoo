"""Report intake enums — the reporter-supplied triage hints.

Both values are stored VERBATIM on the per-code report document
(``reasons`` / ``vectors`` $addToSet arrays). They are triage hints for
the abuse funnel, never inputs to automated action — assessed harm tiers
live in the url-safety architecture, not here.
"""

from enum import Enum
from typing import Literal

# Per-item rejection codes on the POST /api/v1/reports wire. ONE shared
# alias — the service dataclass and the response DTO both annotate with
# this, so a fourth code added to one side but not the other is a type
# error at the source instead of a 500 at response serialization.
RejectionCode = Literal["invalid_input", "not_found", "duplicate_in_batch"]


class ReportReason(str, Enum):
    """Reporter-claimed reason, aligned with the safety framework's harm
    tiers so intake slots into the funnel without a translation layer."""

    PHISHING = "phishing"
    MALWARE = "malware"
    SPAM = "spam"
    ILLEGAL_CONTENT = "illegal_content"
    OTHER = "other"


class ReportVector(str, Enum):
    """How the link reached the reporter — the delivery-vector hint the
    451 page's scam-awareness story needs."""

    SMS = "sms"
    EMAIL = "email"
    DM = "dm"
    SOCIAL = "social"
    WEB = "web"
    OTHER = "other"
