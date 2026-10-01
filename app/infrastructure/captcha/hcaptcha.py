"""hCaptcha implementation of CaptchaProvider.

Ported from utils/contact_utils.py:
- sync requests → async httpx via HttpClient
- module-level os.getenv → injected secret key
- 5-second timeout is enforced by HttpClient default
"""

from app.infrastructure.http_client import HttpClient
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

_HCAPTCHA_VERIFY_URL = "https://hcaptcha.com/siteverify"


class HCaptchaProvider:
    def __init__(self, secret: str, http_client: HttpClient) -> None:
        self._secret = secret
        self._http = http_client

    async def verify(self, token: str) -> bool:
        if not self._secret:
            return True
        try:
            response = await self._http.post(
                _HCAPTCHA_VERIFY_URL,
                data={"response": token, "secret": self._secret},
            )
            if response.status_code == 200:
                data = response.json()
                success = data.get("success", False)
                if not success:
                    log.warning(
                        "hcaptcha_verification_failed",
                        error_codes=data.get("error-codes", []),
                    )
                return bool(success)
            log.error(
                "hcaptcha_api_error",
                status_code=response.status_code,
                response_text=response.text[:200],
            )
            return False
        except Exception as e:
            log.error(
                "hcaptcha_request_failed", error=str(e), error_type=type(e).__name__
            )
            return False
