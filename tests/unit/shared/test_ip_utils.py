from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.shared.ip_utils import get_client_ip

from .conftest import _make_request

# ---------------------------------------------------------------------------
# shared.ip_utils — get_client_ip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers, client_host, expected_ip",
    [
        (
            {"CF-Connecting-IP": "1.2.3.4", "X-Real-IP": "9.9.9.9"},
            "10.0.0.1",
            "1.2.3.4",
        ),
        ({"True-Client-IP": "5.6.7.8"}, "10.0.0.1", "5.6.7.8"),
        ({"X-Forwarded-For": "11.22.33.44, 99.99.99.99"}, "10.0.0.1", "11.22.33.44"),
        ({"X-Real-IP": "55.66.77.88"}, "10.0.0.1", "55.66.77.88"),
        ({}, "192.168.1.50", "192.168.1.50"),  # fallback to client.host
    ],
    ids=[
        "cloudflare",
        "true_client_ip",
        "x_forwarded_for_multi",
        "x_real_ip",
        "fallback",
    ],
)
def test_get_client_ip(headers, client_host, expected_ip):
    assert get_client_ip(_make_request(headers, client_host)) == expected_ip


def test_get_client_ip_no_client_returns_empty():
    req = MagicMock()
    req.headers = {}
    req.client = None
    assert get_client_ip(req) == ""
