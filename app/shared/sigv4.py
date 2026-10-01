"""AWS Signature Version 4 signing — pure stdlib, no boto.

Covers exactly what the R2 client needs: single-request signing with a
known payload hash and an optional pre-canonicalized query string, no
chunked uploads. R2 accepts region "auto". Test vectors: AWS SigV4 test
suite (see the unit tests).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

_ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    host: str,
    path: str,
    payload_hash: str,
    access_key_id: str,
    secret_access_key: str,
    headers: dict[str, str] | None = None,
    query: str = "",
    region: str = "auto",
    service: str = "s3",
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the headers to attach to the request.

    ``path`` must already be URL-encoded with a leading slash. ``query``
    is the canonical query string — key-sorted, URL-encoded, no leading
    ``?`` — or empty for no params. ``payload_hash`` is the sha256
    hexdigest of the request body (of ``b""`` for bodiless requests).
    ``now`` is injectable for tests.
    """
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    date = ts[:8]

    all_headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": ts,
        **{k.lower(): v for k, v in (headers or {}).items()},
    }
    signed_names = ";".join(sorted(all_headers))
    canonical_headers = "".join(
        f"{k}:{all_headers[k].strip()}\n" for k in sorted(all_headers)
    )
    canonical_request = "\n".join(
        [method.upper(), path, query, canonical_headers, signed_names, payload_hash]
    )

    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            ts,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    key = _hmac(f"AWS4{secret_access_key}".encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Authorization": (
            f"{_ALGORITHM} Credential={access_key_id}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        ),
        "x-amz-date": ts,
        "x-amz-content-sha256": payload_hash,
        "host": host,
    }
