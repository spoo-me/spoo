"""URL parsing helpers."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from urllib.parse import urlparse, urlsplit

import idna
import tldextract

# RFC 1035 hostname matcher used by the custom-domains code path.
# Labels: 1-63 chars, [a-z0-9-], no leading/trailing hyphen.
# Total length: ≤ 253.
# TLD: either ≥2 alpha chars OR an ASCII-encoded punycode label (``xn--…``)
# so internationalised TLDs (.中国 → ``xn--fiqs8s``) are accepted.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1F\x7F-\x9F<>\"'`\\]")

# Empty suffix_list_urls pins the bundled PSL snapshot — offline, deterministic
# (cache_dir=None alone disables caching, not the first-call network fetch).
_tld_extractor = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())


def registrable_domain(value: str) -> str:
    """Registrable (PSL) domain of a URL or bare host.

    ``a.b.example.co.uk`` -> ``example.co.uk``. Hosts with no known public
    suffix (localhost, intranet names, IP literals) fall back to the domain
    label itself.
    """
    ext = _tld_extractor(value)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def is_registrable_apex(fqdn: str) -> bool:
    """True when *fqdn* is a registrable apex (no subdomain), PSL-aware so
    multi-part TLDs like ``co.uk`` work."""
    ext = _tld_extractor(fqdn.strip("."))
    return bool(ext.domain) and bool(ext.suffix) and not ext.subdomain


def parse_destination(url: str | None) -> dict | None:
    """Split a destination URL into indexable parts.

    Returns ``{scheme, host, subdomain, registrable_domain}`` or ``None``
    when there is no parseable hostname. Never raises: stamping destination
    parts must never be the reason a link fails to save.

    Normalisation: hostname is lowercased, trailing dot stripped, port and
    userinfo discarded (``https://real.com@evil.com`` keys on ``evil.com``),
    IDN encoded to punycode A-labels. IP literals and suffix-less hosts use
    the host itself as their registrable key.
    """
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    if not host:
        return None
    if any(ord(ch) > 127 for ch in host):
        # UTS-46 via idna (where a browser navigates); the stdlib codec is
        # IDNA-2003 and would mis-key some IDN hosts. Failure keeps the host.
        with contextlib.suppress(idna.IDNAError):
            host = idna.encode(host, uts46=True).decode("ascii")
    ext = _tld_extractor(host)
    if ext.suffix:
        registrable = f"{ext.domain}.{ext.suffix}"
        subdomain = ext.subdomain
    else:
        registrable = host
        subdomain = ""
    return {
        "scheme": parsed.scheme.lower(),
        "host": host,
        "subdomain": subdomain,
        "registrable_domain": registrable,
    }


# Single-URL fields besides long_url that send visitors somewhere. The Mongo
# pipelines and the backfill derive their field lists from this tuple.
SINGLE_DESTINATION_FIELDS: tuple[str, ...] = ("pre_start_url", "expired_redirect_url")


def link_destination_urls_for(link: object) -> list[str]:
    """Every URL a doc or request routes to, read off its fields.

    The one place that knows which fields hold destinations. Readers that
    enumerate by keyword drift the moment a field is added; they call this.
    """
    # Duck-typed like shared.ab_variants.variant_urls, not imported here to
    # dodge a cycle (that module imports schemas.models.url, which imports this).
    variants = getattr(link, "ab_variants", None) or []
    return link_destination_urls(
        getattr(link, "long_url", None),
        geo_rules=getattr(link, "geo_rules", None),
        variants=[v["url"] if isinstance(v, dict) else v.url for v in variants],
        **{
            field: getattr(link, field, None) or None
            for field in SINGLE_DESTINATION_FIELDS
        },
    )


def link_destination_urls(
    long_url: str | None,
    *,
    geo_rules: dict[str, str] | None = None,
    variants: Iterable[str] | None = None,
    pre_start_url: str | None = None,
    expired_redirect_url: str | None = None,
) -> list[str]:
    """Every URL a link can send a visitor to: the main destination, geo
    overrides, A/B variants, the pre-start page a scheduled link shows before
    it goes live, and the page an expired link falls back to. Distinct, first
    occurrence kept."""
    seen: set[str] = set()
    out: list[str] = []
    for url in (
        long_url,
        *(geo_rules or {}).values(),
        *(variants or ()),
        pre_start_url,
        expired_redirect_url,
    ):
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _secondary_parts(urls: Iterable[str], exclude: str) -> list[dict]:
    by_host: dict[str, dict] = {}
    for parts in (parse_destination(u) for u in urls):
        if parts is not None and parts["host"] != exclude:
            by_host.setdefault(parts["host"], parts)
    return [by_host[h] for h in sorted(by_host)]


def secondary_hosts(urls: Iterable[str], *, exclude: str = "") -> list[str]:
    """Distinct parsed hosts of *urls* without *exclude*, sorted."""
    return [p["host"] for p in _secondary_parts(urls, exclude)]


def secondary_registrables(urls: Iterable[str], *, exclude: str = "") -> list[str]:
    """Registrable domain of each host secondary_hosts() returns, same order."""
    return [p["registrable_domain"] for p in _secondary_parts(urls, exclude)]


def extract_hostname(url: str | None) -> str | None:
    """Return hostname from URL, or None if unparseable."""
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def extract_fqdn(url: str) -> str:
    """Return the canonical fqdn from a URL.

    Lowercased, trailing dot stripped, port discarded. Used as the canonical
    domain key across config, cache, and middleware so the same hostname
    always maps to the same string.

    Falls back to ``"localhost"`` for inputs without a parseable host
    (raw paths, garbage strings) — defensive shape for callers that feed
    arbitrary user input.
    """
    host = extract_hostname(url)
    if not host:
        return "localhost"
    return host.lower().rstrip(".")


def split_destination(url: str) -> dict:
    """Split a destination URL into display parts for preview surfaces.

    Single source of truth for the legacy Jinja preview page AND the
    public preview API (the spoo-landing mock mirrors this logic):
    ``{url, domain, path, is_https}`` where ``path`` keeps query and
    fragment and collapses a bare ``"/"`` to ``""``.

    ``urlparse`` raises ``ValueError`` on some malformed inputs (e.g. an
    unclosed IPv6 bracket) and legacy v1 ``url`` values are raw — a public
    endpoint must not 500 on them. Such values fall back to being treated
    as the domain themselves, matching the frontend mock's try/catch.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return {
            "url": url,
            "domain": url.split("/")[0],
            "path": "",
            "is_https": False,
        }
    path = (
        parsed.path
        + ("?" + parsed.query if parsed.query else "")
        + ("#" + parsed.fragment if parsed.fragment else "")
    )
    if path == "/":
        path = ""
    return {
        "url": url,
        "domain": parsed.netloc or parsed.path.split("/")[0],
        "path": path,
        "is_https": parsed.scheme == "https",
    }


def normalise_host(raw: str) -> str:
    """Lenient host-header host: lowercased, dot-stripped, port-stripped.

    Sibling to ``normalise_fqdn``. This is the lenient parse for
    Host-header-style input — it never raises and returns ``""`` for
    unparseable input, whereas ``normalise_fqdn`` strictly validates and
    raises. RFC 3986-safe for bracketed IPv6 literals (``urlsplit`` handles
    ``[::1]:8000`` correctly).
    """
    if not raw:
        return ""
    try:
        parsed = urlsplit(f"//{raw.strip()}").hostname
    except ValueError:
        return ""
    return (parsed or "").rstrip(".").lower()


def is_system_default_host(host: str, system_default_domain: str) -> bool:
    """Return True if *host* is the system-default domain or its ``www.``
    alias.

    Single source of truth for the system-default short-circuit rule,
    shared with the tenant resolver so the two never drift on which host
    forms fold onto the default namespace. *host* is expected to already
    be normalised (see ``normalise_host``).
    """
    return host in (system_default_domain, f"www.{system_default_domain}")


def normalise_fqdn(value: object) -> str:
    """Strict canonical form for custom-domain fqdns.

    Strips whitespace, lowercases, drops a trailing dot, and validates
    against RFC 1035 hostname syntax (with punycode TLD support). Raises
    ``ValueError`` for empty / bad-character / malformed input.

    Single source of truth — used by the document model, request DTO,
    AND the repository so a normalised lookup never misses because the
    persisted form drifted from the input form.
    """
    if value is None:
        raise ValueError("fqdn is required")
    normalised = str(value).strip().lower().rstrip(".")
    if not normalised:
        raise ValueError("fqdn is required")
    if _FORBIDDEN_CHARS.search(normalised):
        raise ValueError(f"fqdn contains forbidden characters: {value!r}")
    if not _HOSTNAME_RE.match(normalised):
        raise ValueError(f"fqdn does not look like a valid hostname: {value!r}")
    return normalised
