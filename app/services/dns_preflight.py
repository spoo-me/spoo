"""Pre-register DNS checks. Resolve customer's CNAME + detect CF-as-DNS
authoritative so we never call CF SaaS for unpropagated records (avoids
CF's 15-min retry backoff)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

_DNS_TIMEOUT_SECS = 3.0
_PUBLIC_RESOLVERS = (("1.1.1.1", "1.0.0.1"), ("8.8.8.8", "8.8.4.4"))


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str = ""


async def check_cname(fqdn: str, expected_target: str) -> PreflightResult:
    """Resolve *fqdn* CNAME against *expected_target*. Falls back to A-record
    comparison when CNAME isn't visible — apex CNAMEs on flattening DNS
    providers (Cloudflare, Route 53 alias, etc.) hide the CNAME RR and
    only return the resolved A records."""
    target = expected_target.strip(".").lower()
    results = await asyncio.gather(
        *(_query(fqdn, "CNAME", ips) for ips in _PUBLIC_RESOLVERS),
        return_exceptions=False,
    )
    matched: list[str] = []
    seen_targets: list[str] = []
    for resolver_ips, observed in zip(_PUBLIC_RESOLVERS, results, strict=True):
        if observed is None:
            continue
        seen_targets.extend(observed)
        if target in observed:
            matched.append(resolver_ips[0])
    if matched:
        return PreflightResult(ok=True)
    if seen_targets:
        observed = sorted(set(seen_targets))[0]
        return PreflightResult(
            ok=False,
            reason=(
                f"Your CNAME points to {observed} - It should point to {target}. "
                f"Update the record at your DNS provider."
            ),
        )

    # No CNAME visible — could be unpropagated, or apex flattening. Check
    # A records: if fqdn's A set overlaps target's A set, flattening is
    # working and the routing is correctly wired.
    fqdn_a = await _resolve_a_pooled(fqdn)
    target_a = await _resolve_a_pooled(target)
    if fqdn_a and target_a and fqdn_a & target_a:
        return PreflightResult(ok=True)

    return PreflightResult(
        ok=False,
        reason=(
            "DNS isn't reaching us yet - This is normal right after adding the "
            "records. Try again in a few minutes."
        ),
    )


async def _query(
    fqdn: str, rtype: str, nameservers: tuple[str, ...]
) -> list[str] | None:
    """Resolve *fqdn* of *rtype* via the given nameservers. None on transport error."""
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = list(nameservers)
    resolver.lifetime = _DNS_TIMEOUT_SECS
    try:
        answer = await asyncio.wait_for(
            resolver.resolve(fqdn, rtype),
            timeout=_DNS_TIMEOUT_SECS,
        )
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.Timeout, asyncio.TimeoutError, dns.exception.DNSException):
        return None
    if rtype == "CNAME":
        return [str(rd.target).rstrip(".").lower() for rd in answer]
    return [str(rd) for rd in answer]


async def _resolve_a_pooled(fqdn: str) -> set[str]:
    """Union of A records seen across all public resolvers."""
    results = await asyncio.gather(
        *(_query(fqdn, "A", ips) for ips in _PUBLIC_RESOLVERS),
        return_exceptions=False,
    )
    out: set[str] = set()
    for observed in results:
        if observed:
            out.update(observed)
    return out


async def uses_cloudflare_dns(fqdn: str) -> bool:
    """True if fqdn's authoritative NS is CF. Customer must grey-cloud
    their records or CF SaaS validation fails."""
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [ip for ips in _PUBLIC_RESOLVERS for ip in ips]
    resolver.lifetime = _DNS_TIMEOUT_SECS
    parts = fqdn.strip(".").lower().split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        try:
            answer = await asyncio.wait_for(
                resolver.resolve(candidate, "NS"),
                timeout=_DNS_TIMEOUT_SECS,
            )
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except (
            dns.exception.Timeout,
            asyncio.TimeoutError,
            dns.exception.DNSException,
        ):
            return False
        for rdata in answer:
            target = str(rdata.target).rstrip(".").lower()
            if target.endswith(".ns.cloudflare.com"):
                return True
        return False
    return False
