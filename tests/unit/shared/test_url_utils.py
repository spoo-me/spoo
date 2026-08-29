"""Unit tests for shared/url_utils.py — extract_hostname + extract_fqdn + normalise_fqdn."""

import pytest

from shared.url_utils import (
    extract_fqdn,
    extract_hostname,
    is_registrable_apex,
    normalise_fqdn,
    parse_destination,
    registrable_domain,
)


class TestExtractHostname:
    def test_returns_hostname_from_full_url(self):
        assert extract_hostname("https://spoo.me/path") == "spoo.me"

    def test_returns_hostname_with_port(self):
        # urlparse strips the port from .hostname
        assert extract_hostname("http://localhost:8000/abc") == "localhost"

    def test_returns_none_for_empty(self):
        assert extract_hostname(None) is None
        assert extract_hostname("") is None

    def test_returns_none_for_unparseable(self):
        # urllib's urlparse is forgiving but a string with no scheme and no
        # netloc structure resolves to ``hostname=None``.
        assert extract_hostname("not a url at all") is None


class TestExtractFqdn:
    def test_lowercases(self):
        assert extract_fqdn("HTTPS://SPOO.ME/abc") == "spoo.me"

    def test_strips_trailing_dot(self):
        # Fully qualified DNS notation includes a trailing dot for the root.
        assert extract_fqdn("https://spoo.me./abc") == "spoo.me"

    def test_strips_port(self):
        assert extract_fqdn("https://spoo.me:8443/x") == "spoo.me"

    def test_handles_subdomain(self):
        assert extract_fqdn("https://links.acme.com/x") == "links.acme.com"

    def test_self_hoster_url(self):
        assert extract_fqdn("https://my.shortener.dev") == "my.shortener.dev"

    def test_falls_back_to_localhost_for_no_host(self):
        # Defensive fallback for callers fed user-supplied URLs that lack
        # a parseable host (raw paths, garbage strings).
        assert extract_fqdn("") == "localhost"
        assert extract_fqdn("not-a-url") == "localhost"

    def test_idempotent(self):
        # Two calls with equivalent inputs return identical strings — needed
        # so the cache key, the seeded custom_domains row, and the request
        # middleware all agree on the canonical form.
        assert extract_fqdn("HTTPS://Spoo.Me./") == extract_fqdn("https://spoo.me")


class TestNormaliseFqdn:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("links.acme.com", "links.acme.com"),
            ("LINKS.ACME.COM", "links.acme.com"),
            ("  links.acme.com  ", "links.acme.com"),
            ("links.acme.com.", "links.acme.com"),
            ("acme.co", "acme.co"),
            # Punycode TLD (encoded `.中国`) — required for IDN custom domains.
            ("links.xn--fiqs8s", "links.xn--fiqs8s"),
            # Multi-level subdomain
            ("a.b.c.example.com", "a.b.c.example.com"),
        ],
    )
    def test_accepts_valid_inputs(self, value, expected):
        assert normalise_fqdn(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "no_underscores_allowed.com",
            "-leading-hyphen.com",
            "trailing-hyphen-.com",
            "single-label",
            "two..consecutive.dots.com",
            "evil<script>.com",
            "evil`backtick.com",
            "evil\\backslash.com",
            "evil\x00null.com",
            "a" * 64 + ".com",  # label > 63 chars
            "a" * 254 + ".com",  # total > 253 chars
        ],
    )
    def test_rejects_invalid_inputs(self, value):
        with pytest.raises(ValueError):
            normalise_fqdn(value)


class TestParseDestination:
    def test_simple_https_url(self):
        parts = parse_destination("https://example.com/path?q=1")
        assert parts == {
            "scheme": "https",
            "host": "example.com",
            "subdomain": "",
            "registrable_domain": "example.com",
        }

    def test_subdomain_split(self):
        parts = parse_destination("http://a.b.example.co.uk/x")
        assert parts["host"] == "a.b.example.co.uk"
        assert parts["subdomain"] == "a.b"
        assert parts["registrable_domain"] == "example.co.uk"

    def test_userinfo_spoof_uses_real_host(self):
        # https://www.instagram.com@spoo.me/x — the instagram part is
        # userinfo, the real host is spoo.me. Observed in a live campaign.
        parts = parse_destination("https://www.instagram.com@spoo.me/MrNpzIk")
        assert parts["host"] == "spoo.me"
        assert parts["registrable_domain"] == "spoo.me"

    def test_ip_literal_is_its_own_key(self):
        parts = parse_destination("http://93.184.216.34:8080/x")
        assert parts["host"] == "93.184.216.34"
        assert parts["registrable_domain"] == "93.184.216.34"
        assert parts["subdomain"] == ""

    def test_idn_normalised_to_punycode(self):
        parts = parse_destination("https://münchen.de/x")
        assert parts["host"] == "xn--mnchen-3ya.de"
        assert parts["registrable_domain"] == "xn--mnchen-3ya.de"

    def test_uppercase_and_trailing_dot_normalised(self):
        parts = parse_destination("HTTPS://ExAmPle.COM./x")
        assert parts["scheme"] == "https"
        assert parts["host"] == "example.com"

    def test_port_discarded(self):
        assert parse_destination("https://example.com:8443/")["host"] == "example.com"

    def test_unparseable_returns_none(self):
        assert parse_destination("not a url") is None
        assert parse_destination("") is None
        assert parse_destination(None) is None
        assert parse_destination("https://[::1") is None  # ValueError path

    def test_no_suffix_host_falls_back_to_host(self):
        parts = parse_destination("http://localhost:8000/x")
        assert parts["registrable_domain"] == "localhost"


class TestRegistrableDomain:
    def test_url_input(self):
        assert registrable_domain("https://a.b.example.com/x") == "example.com"

    def test_bare_host(self):
        assert registrable_domain("news.bbc.co.uk") == "bbc.co.uk"

    def test_no_suffix_returns_domain_part(self):
        assert registrable_domain("localhost") == "localhost"


class TestIsRegistrableApex:
    def test_apex(self):
        assert is_registrable_apex("example.co.uk") is True

    def test_subdomain_is_not_apex(self):
        assert is_registrable_apex("go.example.co.uk") is False

    def test_bare_label_is_not_apex(self):
        assert is_registrable_apex("localhost") is False
