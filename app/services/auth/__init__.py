"""
Auth service package — split by domain concern.

Re-exports all service classes and constants for convenient imports.
"""

from app.services.auth.credentials import CredentialService
from app.services.auth.device import (
    APP_ID_MAX_LEN,
    DEVICE_AUTH_EXPIRY_SECONDS,
    DeviceAuthService,
)
from app.services.auth.otp import (
    MAX_TOKENS_PER_HOUR,
    MAX_VERIFICATION_ATTEMPTS,
    OTP_EXPIRY_SECONDS,
    OtpService,
)
from app.services.auth.password import PasswordService
from app.services.auth.verification import EmailVerificationService

__all__ = [
    "APP_ID_MAX_LEN",
    "DEVICE_AUTH_EXPIRY_SECONDS",
    "MAX_TOKENS_PER_HOUR",
    "MAX_VERIFICATION_ATTEMPTS",
    "OTP_EXPIRY_SECONDS",
    "CredentialService",
    "DeviceAuthService",
    "EmailVerificationService",
    "OtpService",
    "PasswordService",
]
