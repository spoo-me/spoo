"""SSRF-guarded outbound fetch for user-supplied URLs.

The shared HttpClient is unusable here: it hardcodes follow_redirects=True,
which would let httpx follow hops with zero per-hop validation. This module
owns its redirect loop and, per hop:

  1. requires https;
  2. resolves A+AAAA first (dnspython, precedent services/dns_preflight.py)
     and rejects unless ALL addresses are public — private/loopback/
     link-local/reserved/multicast/unspecified, with IPv4-mapped IPv6
     (::ffff:10.0.0.1) unwrapped;
  3. connects to the RESOLVED IP (host header + SNI carry the original
     name) so DNS can't rebind between check and fetch;
  4. streams the body under a hard byte cap.

Used by the async og:image validator (Phase C) and the /api/v1/metadata
destination parser (Phase D).
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import httpx

from infrastructure.logging import get_logger

log = get_logger(__name__)

# Default only — deployments configure META_TAGS_FETCH_USER_AGENT (config.py).
DEFAULT_USER_AGENT = "spoo.me-og-validator/1.0 (+https://spoo.me)"
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class FetchHardError(Exception):
    """Permanent: the URL can never validate (private IP, wrong type, 4xx…)."""


class FetchDeniedError(FetchHardError):
    """The origin refused OUR client (401/403) — typically a WAF or hotlink
    protection blocking an unrecognized User-Agent. Says nothing about
    whether the resource works for preview crawlers, whose UAs are widely
    allowlisted; callers that act on fetch results (e.g. clearing a user's
    og:image) should treat this as indeterminate, not broken."""


class FetchTransientError(Exception):
    """Retryable: timeouts, 5xx, 429, DNS timeouts."""


@dataclass(frozen=True)
class FetchedBody:
    data: bytes
    content_type: str
    final_url: str


_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        # NAT64 (RFC 6052) reports is_global=True, but on a NAT64/DNS64
        # host 64:ff9b::7f00:1 reaches 127.0.0.1 — reject the whole
        # prefix; a translation address is never a legitimate target.
        elif addr in _NAT64_PREFIX:
            return False
    # is_global (not a flag union) catches CGNAT 100.64.0.0/10 and
    # transitional ranges the union missed; exclude multicast, which is
    # is_global=True but must never be a fetch target.
    return addr.is_global and not addr.is_multicast


async def resolve_public_ip(host: str) -> str:
    """Resolve *host* and return one address, rejecting any private result."""
    # A literal IP address skips DNS entirely.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _is_public(host):
            raise FetchHardError("address is not public")
        return host

    ips: list[str] = []
    for rdtype in ("A", "AAAA"):
        try:
            answer = await dns.asyncresolver.resolve(host, rdtype)
            ips.extend(r.to_text() for r in answer)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.Timeout as exc:
            raise FetchTransientError("DNS timeout") from exc
        except dns.exception.DNSException as exc:
            raise FetchHardError(f"DNS failure: {type(exc).__name__}") from exc
    if not ips:
        raise FetchHardError("host does not resolve")
    if not all(_is_public(ip) for ip in ips):
        # ANY private address fails the whole host — a mixed record set is
        # exactly what a rebinding/split-horizon attack looks like.
        raise FetchHardError("host resolves to a non-public address")
    return ips[0]


def _bracket(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


# The ONE SSRF resolver: outbound callers reuse this instead of growing a
# second guard that drifts from the rebinding rules here.
bracket_ip = _bracket


async def _read_body(
    resp: httpx.Response, max_bytes: int, truncate_over_cap: bool
) -> bytearray:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > max_bytes:
            if truncate_over_cap:
                return buf[:max_bytes]
            raise FetchHardError("body over cap")
    return buf


async def fetch_public(
    url: str,
    *,
    accept_content: tuple[str, ...],
    reject_content: tuple[str, ...] = (),
    timeout: float = 5.0,
    max_bytes: int = 1_048_576,
    max_redirects: int = 3,
    truncate_over_cap: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchedBody:
    """Fetch *url* with SSRF guards. ``accept_content`` are content-type
    prefixes (e.g. ``("image/",)``); ``reject_content`` are substrings that
    fail even when a prefix matched (e.g. ``("svg",)``).

    ``truncate_over_cap=True`` returns the first ``max_bytes`` instead of
    failing when the body exceeds the cap — right for HTML meta parsing
    (tags live in <head>; github.com's homepage alone is >512KB), wrong
    for images (a truncated image is not a valid image)."""
    # httpx timeouts are per-operation and reset each chunk, so the body
    # read below is bounded by an explicit wall-clock ceiling instead.
    hop_deadline = timeout * 3
    for _hop in range(max_redirects + 1):
        parsed = httpx.URL(url)
        if parsed.scheme != "https":
            raise FetchHardError("non-https URL")
        ip = await resolve_public_ip(parsed.host)

        # Pin the connection to the validated IP; keep name-based TLS via
        # sni_hostname and the Host header.
        pinned = parsed.copy_with(host=_bracket(ip))
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            request = client.build_request(
                "GET",
                pinned,
                headers={
                    "Host": parsed.host,
                    "User-Agent": user_agent,
                    # No gzip: the byte cap counts decompressed bytes, so a
                    # small compressed bomb could blow past it in one read.
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": parsed.host},
            )
            try:
                resp = await client.send(request, stream=True)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise FetchTransientError(str(exc)) from exc

            try:
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchHardError("redirect without location")
                    url = str(parsed.join(location))
                    continue  # next hop re-validated from the top
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise FetchTransientError(f"status {resp.status_code}")
                if resp.status_code in (401, 403):
                    raise FetchDeniedError(f"status {resp.status_code}")
                if resp.status_code != 200:
                    raise FetchHardError(f"status {resp.status_code}")

                ctype = (
                    resp.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if not ctype.startswith(accept_content):
                    raise FetchHardError(f"content-type {ctype!r}")
                if any(marker in ctype for marker in reject_content):
                    raise FetchHardError(f"content-type {ctype!r}")
                declared = resp.headers.get("content-length", "")
                if (
                    declared.isdigit()  # garbage header ≠ worker crash
                    and int(declared) > max_bytes
                    and not truncate_over_cap
                ):
                    raise FetchHardError("content-length over cap")

                # wait_for (not asyncio.timeout — 3.10 support) is the
                # wall-clock ceiling a slow-drip server can't evade.
                try:
                    buf = await asyncio.wait_for(
                        _read_body(resp, max_bytes, truncate_over_cap),
                        timeout=hop_deadline,
                    )
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    raise FetchTransientError("read deadline exceeded") from exc
                return FetchedBody(bytes(buf), ctype, str(parsed))
            finally:
                await resp.aclose()
    raise FetchHardError("too many redirects")


async def fetch_public_image(
    url: str,
    *,
    timeout: float = 5.0,
    max_bytes: int = 1_048_576,
    max_redirects: int = 3,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchedBody:
    return await fetch_public(
        url,
        accept_content=("image/",),
        reject_content=("svg",),
        timeout=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        user_agent=user_agent,
    )


@dataclass(frozen=True)
class PostResult:
    """Outcome of a webhook-style POST. Status outcomes are DATA here
    (the delivery executor owns retry policy) — only SSRF violations and
    transport errors surface as fields, never exceptions."""

    status_code: int | None
    error: str | None
    body_snippet: str | None  # first 256 bytes, for the delivery log
    # Parsed integer Retry-After, when the receiver sent one (429/503 flow
    # control). HTTP-date form is not parsed — callers fall back to their
    # own delay.
    retry_after_seconds: float | None = None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


async def post_public(
    url: str,
    body: str,
    *,
    headers: dict[str, str],
    timeout: float = 15.0,
    snippet_bytes: int = 256,
) -> PostResult:
    """POST *body* to *url* with the same SSRF guards as fetch_public.

    No redirects are followed — a redirect defeats IP pinning, so it is
    reported as an error outcome. Never raises: webhook delivery failure
    is a recorded fact, not an exception path.
    """
    try:
        parsed = httpx.URL(url)
        if parsed.scheme != "https":
            return PostResult(None, "non-https URL", None)
        ip = await resolve_public_ip(parsed.host)
    except (FetchHardError, FetchTransientError, httpx.InvalidURL) as exc:
        # Transient DNS failures included: delivery outcomes are DATA for
        # the retry ladder, never exceptions that skip attempt recording.
        return PostResult(None, str(exc), None)

    pinned = parsed.copy_with(host=_bracket(ip))
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            request = client.build_request(
                "POST",
                pinned,
                content=body.encode(),
                headers={"Host": parsed.host, **headers},
                extensions={"sni_hostname": parsed.host},
            )
            resp = await client.send(request, stream=True)
            try:
                if resp.status_code in _REDIRECT_STATUSES:
                    return PostResult(resp.status_code, "redirect not followed", None)
                try:
                    buf = await asyncio.wait_for(
                        _read_body(resp, snippet_bytes, True), timeout=timeout
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    buf = bytearray()
                snippet = bytes(buf).decode("utf-8", errors="replace") if buf else None
                return PostResult(
                    resp.status_code,
                    None,
                    snippet,
                    _parse_retry_after(resp.headers.get("retry-after")),
                )
            finally:
                await resp.aclose()
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return PostResult(None, f"{type(exc).__name__}: {exc}", None)


async def validate_public_https_url(url: str) -> None:
    """Creation-time gate for user-registered outbound URLs (webhook
    endpoints): https + all resolved addresses public. Raises
    FetchHardError with a user-safe message otherwise."""
    parsed = httpx.URL(url)
    if parsed.scheme != "https":
        raise FetchHardError("URL must be https")
    await resolve_public_ip(parsed.host)


@dataclass(frozen=True)
class ChainHop:
    url: str
    status: int | None  # None: this hop never answered


@dataclass(frozen=True)
class ExpandedChain:
    hops: list[ChainHop]
    final_url: str
    final_status: int | None
    truncated: bool


async def expand_public(
    url: str,
    *,
    timeout: float = 5.0,
    max_redirects: int = 10,
    user_agent: str = DEFAULT_USER_AGENT,
) -> ExpandedChain:
    """Follow *url*'s redirect chain hop by hop and report every stop.

    Unlike fetch_public this never reads bodies and allows plain-http
    hops — real shortener chains bounce through http trackers, and only
    headers ever ride the wire here. Every hop still gets the full SSRF
    guard: public-DNS resolution, IP pinning, no auto-redirects.
    """
    hops: list[ChainHop] = []
    for _hop in range(max_redirects + 1):
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https"):
            raise FetchHardError("unsupported scheme")
        try:
            ip = await resolve_public_ip(parsed.host)
        except FetchHardError:
            if not hops:
                raise
            # Mid-chain dead end (NXDOMAIN, private space): report the
            # chain up to it rather than erasing what we learned.
            hops.append(ChainHop(url, None))
            return ExpandedChain(hops, url, None, False)
        pinned = parsed.copy_with(host=_bracket(ip))
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            request = client.build_request(
                "GET",
                pinned,
                headers={
                    "Host": parsed.host,
                    "User-Agent": user_agent,
                    "Accept-Encoding": "identity",
                },
                extensions=(
                    {"sni_hostname": parsed.host} if parsed.scheme == "https" else {}
                ),
            )
            try:
                resp = await client.send(request, stream=True)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if not hops:
                    raise FetchTransientError(str(exc)) from exc
                hops.append(ChainHop(url, None))
                return ExpandedChain(hops, url, None, False)
            try:
                status = resp.status_code
                hops.append(ChainHop(str(parsed), status))
                if status in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        return ExpandedChain(hops, str(parsed), status, False)
                    url = str(parsed.join(location))
                    continue
                return ExpandedChain(hops, str(parsed), status, False)
            finally:
                await resp.aclose()
    return ExpandedChain(hops, url, None, True)
