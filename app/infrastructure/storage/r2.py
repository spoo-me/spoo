"""Cloudflare R2 storage client (S3-compatible API) — httpx + SigV4.

House style follows infrastructure/cloudflare_kv.py: thin REST client on
the shared HttpClient with an ``is_configured`` gate. Deliberate contrast
with the best-effort KV client: ``put_object`` RAISES on failure — it sits
on the request path of a user write, and silently storing a broken image
URL would be worse than a 502.

Zero new dependencies: SigV4 is ~50 lines of stdlib (see shared/sigv4.py);
boto3/aioboto3 were rejected (dependency tree + lifecycle mismatch).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

from app.errors import R2StorageError
from app.infrastructure.logging import get_logger
from app.shared.sigv4 import sigv4_headers

if TYPE_CHECKING:
    from app.infrastructure.http_client import HttpClient

log = get_logger(__name__)

# Keys are content-addressed (og/{owner}/{sha256}.{ext}) so objects are
# immutable — let the CDN in front of the bucket and platform caches hold
# them forever. Stored as object metadata, served on every GET.
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Defence-in-depth for user-supplied bytes: never render as a document.
# nosniff also belongs in a CF Transform Rule on the custom domain (R2
# doesn't guarantee echoing it) — this is the belt to that braces.
_INLINE_DISPOSITION = "inline"
_NOSNIFF = "nosniff"

# Local MinIO-style dev endpoints may skip TLS; everything else must not.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _parse_list_objects(xml_text: str) -> tuple[list[str], bool, str | None]:
    """Extract (keys, is_truncated, continuation_token) from a
    ListObjectsV2 response. Namespace-agnostic — R2 sometimes omits the
    S3 xmlns the AWS docs show."""
    root = ElementTree.fromstring(xml_text)
    keys: list[str] = []
    truncated = False
    token: str | None = None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Key" and el.text:
            keys.append(el.text)
        elif tag == "IsTruncated":
            truncated = (el.text or "").strip().lower() == "true"
        elif tag == "NextContinuationToken":
            token = el.text
    return keys, truncated, token


class R2StorageClient:
    def __init__(
        self,
        *,
        http_client: HttpClient,
        account_id: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        bucket: str | None,
        public_base_url: str | None,
        endpoint_url: str | None = None,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        self._http = http_client
        self._account_id = account_id
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._bucket = bucket
        self._public_base_url = (public_base_url or "").rstrip("/")
        self._endpoint = (
            endpoint_url.rstrip("/")
            if endpoint_url
            else f"https://{account_id}.r2.cloudflarestorage.com"
        )
        parsed = urlparse(self._endpoint)
        # SigV4 signs but never encrypts — a plain-http endpoint would put
        # object bytes and credentials-derived signatures on the wire.
        if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                "R2 endpoint_url must be https:// (plain http is allowed "
                "only for localhost MinIO-style dev endpoints)"
            )
        self._host = parsed.netloc
        self._timeout = request_timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(
            self._account_id
            and self._access_key_id
            and self._secret_access_key
            and self._bucket
            and self._public_base_url
        )

    def public_url(self, key: str) -> str:
        return f"{self._public_base_url}/{key}"

    async def put_object(self, key: str, data: bytes, *, content_type: str) -> str:
        """Upload and return the public URL. Raises R2StorageError on failure."""
        path = f"/{self._bucket}/{quote(key, safe='/')}"
        object_headers = {
            "content-type": content_type,
            "cache-control": _IMMUTABLE_CACHE_CONTROL,
            "content-disposition": _INLINE_DISPOSITION,
            "x-content-type-options": _NOSNIFF,
        }
        signed = sigv4_headers(
            method="PUT",
            host=self._host,
            path=path,
            payload_hash=hashlib.sha256(data).hexdigest(),
            access_key_id=self._access_key_id or "",
            secret_access_key=self._secret_access_key or "",
            headers=object_headers,
        )
        try:
            resp = await self._http.request(
                "PUT",
                f"{self._endpoint}{path}",
                content=data,
                headers={**signed, **object_headers},
                # The shared client's 5s default is too tight for image PUTs.
                timeout=self._timeout,
            )
        except Exception as exc:
            log.error("r2_put_failed", key=key, error=str(exc))
            raise R2StorageError("Image upload failed") from exc
        if resp.status_code >= 300:
            log.error(
                "r2_put_failed",
                key=key,
                status=resp.status_code,
                body_preview=resp.text[:300],
            )
            raise R2StorageError("Image upload failed")
        log.info("r2_put_succeeded", key=key, bytes=len(data))
        return self.public_url(key)

    async def list_keys(self, prefix: str) -> list[str]:
        """List every object key under *prefix* (paginated ListObjectsV2).

        Powers the account-erasure R2 sweep. RAISES on failure like
        ``put_object`` — a silent partial listing would let an erasure
        claim completeness it doesn't have.
        """
        path = f"/{self._bucket}"
        keys: list[str] = []
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            # Canonical query string: key-sorted, strict URL-encoding.
            query = "&".join(
                f"{quote(k, safe='')}={quote(v, safe='')}"
                for k, v in sorted(params.items())
            )
            signed = sigv4_headers(
                method="GET",
                host=self._host,
                path=path,
                query=query,
                payload_hash=hashlib.sha256(b"").hexdigest(),
                access_key_id=self._access_key_id or "",
                secret_access_key=self._secret_access_key or "",
            )
            try:
                resp = await self._http.request(
                    "GET", f"{self._endpoint}{path}?{query}", headers=signed
                )
            except Exception as exc:
                log.error("r2_list_failed", prefix=prefix, error=str(exc))
                raise R2StorageError("Object listing failed") from exc
            if resp.status_code >= 300:
                log.error(
                    "r2_list_failed",
                    prefix=prefix,
                    status=resp.status_code,
                    body_preview=resp.text[:300],
                )
                raise R2StorageError("Object listing failed")
            page, truncated, token = _parse_list_objects(resp.text)
            keys.extend(page)
            if not truncated or not token:
                return keys

    async def delete_object(self, key: str) -> bool:
        """Best-effort delete (takedown tooling / future GC). 404 = success."""
        path = f"/{self._bucket}/{quote(key, safe='/')}"
        signed = sigv4_headers(
            method="DELETE",
            host=self._host,
            path=path,
            payload_hash=hashlib.sha256(b"").hexdigest(),
            access_key_id=self._access_key_id or "",
            secret_access_key=self._secret_access_key or "",
        )
        try:
            resp = await self._http.request(
                "DELETE", f"{self._endpoint}{path}", headers=signed
            )
        except Exception as exc:
            log.warning("r2_delete_failed", key=key, error=str(exc))
            return False
        ok = resp.status_code < 300 or resp.status_code == 404
        if not ok:
            log.warning("r2_delete_failed", key=key, status=resp.status_code)
        return ok
