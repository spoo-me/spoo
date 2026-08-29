"""Domain intelligence behind GET /api/v1/domain-intel (the URL expander
tool's records panel): DNS records, RDAP registration data, and the TLS
certificate of a destination host. Read-only public facts, cached a day —
none of it changes faster, and RDAP registries rate-limit hard."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from datetime import datetime, timezone

import dns.asyncresolver
import dns.exception
import httpx
import tldextract

from infrastructure.cache.meta_fetch_cache import MetaFetchCache
from infrastructure.http_client import HttpClient
from infrastructure.logging import get_logger
from infrastructure.safe_fetch import resolve_public_ip

log = get_logger(__name__)

_tld_extractor = tldextract.TLDExtract(cache_dir=None)

_DNS_TYPES = ("A", "AAAA", "MX", "NS", "TXT")
_MAX_RECORDS = 8
_TLS_TIMEOUT = 5.0


async def _resolve_type(host: str, rdtype: str) -> list[str]:
    try:
        answer = await dns.asyncresolver.resolve(host, rdtype)
        return [r.to_text().strip('"') for r in answer][:_MAX_RECORDS]
    except dns.exception.DNSException:
        return []


def _rdap_event(events: list[dict], action: str) -> str | None:
    for event in events:
        if event.get("eventAction") == action:
            return event.get("eventDate")
    return None


def _rdap_registrar(entities: list[dict]) -> str | None:
    for entity in entities:
        if "registrar" not in entity.get("roles", []):
            continue
        for prop in entity.get("vcardArray", [None, []])[1]:
            if prop and prop[0] == "fn" and prop[3]:
                return str(prop[3])
    return None


def _age_days(created: str | None) -> int | None:
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except ValueError:
        return None


def _cert_name(rdns: tuple) -> dict[str, str]:
    return {k: v for rdn in rdns for (k, v) in rdn}


def _cert_summary(cert: dict) -> dict:
    """Map a peer certificate onto the wire shape."""
    issuer = _cert_name(cert.get("issuer", ()))
    subject = _cert_name(cert.get("subject", ()))
    not_after = cert.get("notAfter")
    days_left = None
    if not_after:
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            )
            days_left = (expires - datetime.now(timezone.utc)).days
        except ValueError:
            pass
    sans = [v for (k, v) in cert.get("subjectAltName", ()) if k == "DNS"]
    return {
        "issuer": issuer.get("organizationName") or issuer.get("commonName"),
        "subject": subject.get("commonName"),
        "valid_from": cert.get("notBefore"),
        "valid_to": not_after,
        "days_left": days_left,
        "sans": sans[:_MAX_RECORDS],
    }


class DomainIntelService:
    def __init__(self, cache: MetaFetchCache, http_client: HttpClient) -> None:
        self._cache = cache
        self._http = http_client

    async def lookup(self, host: str) -> dict:
        cached = await self._cache.get(host)
        if cached is not None:
            return cached

        # Same SSRF guard the fetcher runs: the caller picks this hostname,
        # and a public name can resolve into private space. Raises unless
        # every address is public, so the TLS probe below can't be aimed
        # at the internal network.
        public_ip = await resolve_public_ip(host)

        registrable = _tld_extractor(host).registered_domain or host

        dns_results, whois, cert = await asyncio.gather(
            self._dns(host),
            self._rdap(registrable),
            self._tls(host, public_ip),
        )

        payload = {
            "host": host,
            "registrable_domain": registrable,
            "dns": dns_results,
            "whois": whois,
            "ssl": cert,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._cache.set(host, payload)
        return payload

    async def _dns(self, host: str) -> dict[str, list[str]]:
        results = await asyncio.gather(*(_resolve_type(host, t) for t in _DNS_TYPES))
        return {t.lower(): r for t, r in zip(_DNS_TYPES, results, strict=True)}

    async def _rdap(self, domain: str) -> dict | None:
        """rdap.org bootstraps to the registry's own RDAP server; a miss
        (unsupported TLD, registry down) is data we simply don't show."""
        try:
            resp = await self._http.get(
                f"https://rdap.org/domain/{domain}",
                # The bootstrap hands off to the registry, so a couple of
                # hops are expected — but it isn't ours, so cap them.
                extensions={"max_redirects": 3},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.info("domain_intel_rdap_miss", domain=domain, reason=str(exc))
            return None
        events = data.get("events", [])
        created = _rdap_event(events, "registration")
        return {
            "registrar": _rdap_registrar(data.get("entities", [])),
            "created": created,
            "updated": _rdap_event(events, "last changed"),
            "expires": _rdap_event(events, "expiration"),
            "age_days": _age_days(created),
        }

    async def _tls(self, host: str, ip: str) -> dict | None:
        """Handshake against the pre-validated address, with the hostname
        carried in SNI so the certificate still verifies (and DNS can't
        rebind between the guard and the connection)."""
        try:
            ctx = ssl.create_default_context()
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, 443, ssl=ctx, server_hostname=host),
                timeout=_TLS_TIMEOUT,
            )
        except (OSError, ssl.SSLError, asyncio.TimeoutError, TimeoutError):
            return None
        try:
            cert = writer.get_extra_info("peercert") or {}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return _cert_summary(cert) if cert else None
