"""Unit tests for CfSaasBackend (HostnameRegistrar + DomainVerifier + EdgeProvisioner)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from app.errors import CloudflareAPIError
from app.infrastructure.cloudflare_client import CFHostnameResult
from app.services.cf_saas_backend import CfSaasBackend


def _backend(
    *,
    cf_client: MagicMock | None = None,
    repo: MagicMock | None = None,
) -> CfSaasBackend:
    return CfSaasBackend(
        cf_client=cf_client or MagicMock(),
        custom_domain_repo=repo or MagicMock(),
        cname_target="customers.spoo.me",
        dcv_delegation_target="abc.dcv.cloudflare.com",
    )


class TestRegister:
    async def test_register_delegated_dcv_returns_two_records(self):
        cf_client = MagicMock()
        cf_client.create_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-1",
                hostname="links.acme.com",
                status="pending",
                ssl_status="pending_validation",
            )
        )
        backend = _backend(cf_client=cf_client)

        result = await backend.register("links.acme.com", dcv_method="cf_delegated_dcv")

        assert result.backend_id == "cf-1"
        assert result.backend_metadata == {
            "cf_status": "pending",
            "cf_ssl_status": "pending_validation",
        }
        # One routing CNAME + one delegation CNAME.
        assert len(result.instructions) == 2
        cname, delegation = result.instructions
        assert cname["name"] == "links.acme.com"
        assert cname["value"] == "customers.spoo.me"
        assert delegation["name"] == "_acme-challenge.links.acme.com"
        assert delegation["value"] == "links.acme.com.abc.dcv.cloudflare.com"
        # CF API was called with the delegated DCV method; no custom_origin_server
        # — dispatch goes through the zone fallback_origin.
        cf_client.create_custom_hostname.assert_awaited_once_with(
            "links.acme.com",
            dcv_method="txt",
        )

    async def test_delegation_target_with_stray_dots_is_normalised(self):
        cf_client = MagicMock()
        cf_client.create_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-1",
                hostname="links.acme.com",
                status="pending",
                ssl_status="initializing",
            )
        )
        from app.services.cf_saas_backend import CfSaasBackend

        backend = CfSaasBackend(
            cf_client=cf_client,
            custom_domain_repo=MagicMock(),
            cname_target=".customers.spoo.me.",
            dcv_delegation_target=".abc.dcv.cloudflare.com.",
        )
        result = await backend.register("links.acme.com", dcv_method="cf_delegated_dcv")
        cname, delegation = result.instructions
        assert cname["value"] == "customers.spoo.me"
        assert delegation["value"] == "links.acme.com.abc.dcv.cloudflare.com"

    async def test_register_http_dcv_returns_one_record(self):
        cf_client = MagicMock()
        cf_client.create_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-2",
                hostname="links.acme.com",
                status="pending",
                ssl_status="initializing",
            )
        )
        backend = _backend(cf_client=cf_client)
        result = await backend.register("links.acme.com", dcv_method="cf_http_dcv")
        assert len(result.instructions) == 1
        cf_client.create_custom_hostname.assert_awaited_once_with(
            "links.acme.com",
            dcv_method="http",
        )

    async def test_register_appends_ownership_txt_when_cf_returns_it(self):
        cf_client = MagicMock()
        cf_client.create_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-3",
                hostname="links.acme.com",
                status="pending",
                ssl_status="initializing",
                ownership_verification={
                    "type": "txt",
                    "name": "_cf-custom-hostname.links.acme.com",
                    "value": "7f3e4a92-uuid",
                },
            )
        )
        backend = _backend(cf_client=cf_client)
        result = await backend.register("links.acme.com", dcv_method="cf_http_dcv")
        # Routing CNAME + ownership TXT.
        assert len(result.instructions) == 2
        txt = result.instructions[1]
        assert txt["type"] == "TXT"
        assert txt["name"] == "_cf-custom-hostname.links.acme.com"
        assert txt["value"] == "7f3e4a92-uuid"
        assert "ownership" in txt["purpose"].lower()


class TestVerify:
    async def test_verify_active_active_returns_true(self):
        cf_client = MagicMock()
        cf_client.get_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-1",
                hostname="links.acme.com",
                status="active",
                ssl_status="active",
            )
        )
        result = await _backend(cf_client=cf_client).verify(
            "links.acme.com", token="cf-1"
        )
        assert result.verified is True

    async def test_verify_pending_returns_false_with_status_reason(self):
        cf_client = MagicMock()
        cf_client.get_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-1",
                hostname="links.acme.com",
                status="pending",
                ssl_status="pending_validation",
            )
        )
        result = await _backend(cf_client=cf_client).verify(
            "links.acme.com", token="cf-1"
        )
        assert result.verified is False
        assert "pending" in result.reason

    async def test_verify_surfaces_cf_validation_errors(self):
        cf_client = MagicMock()
        cf_client.get_custom_hostname = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-1",
                hostname="links.acme.com",
                status="pending",
                ssl_status="pending_validation",
                verification_errors=["DCV record missing"],
            )
        )
        result = await _backend(cf_client=cf_client).verify(
            "links.acme.com", token="cf-1"
        )
        assert "DCV record missing" in result.reason

    async def test_verify_no_token_short_circuits_false(self):
        backend = _backend()
        result = await backend.verify("links.acme.com", token=None)
        assert result.verified is False
        assert "missing cf_hostname_id" in result.reason

    async def test_verify_translates_cf_api_error(self):
        cf_client = MagicMock()
        cf_client.get_custom_hostname = AsyncMock(
            side_effect=CloudflareAPIError("boom")
        )
        result = await _backend(cf_client=cf_client).verify(
            "links.acme.com", token="cf-1"
        )
        assert result.verified is False
        assert "boom" in result.reason

    async def test_verify_translates_network_error(self):
        cf_client = MagicMock()
        cf_client.get_custom_hostname = AsyncMock(
            side_effect=httpx.ConnectError("dns down")
        )
        result = await _backend(cf_client=cf_client).verify(
            "links.acme.com", token="cf-1"
        )
        assert result.verified is False
        assert "network error" in result.reason


class TestAnnounceRevoked:
    async def test_uses_cf_id_from_doc(self):
        cf_client = MagicMock()
        cf_client.delete_custom_hostname = AsyncMock(return_value=True)
        repo = MagicMock()
        doc = MagicMock()
        doc.cf_hostname_id = "cf-1"
        repo.find_by_fqdn = AsyncMock(return_value=doc)

        ok = await _backend(cf_client=cf_client, repo=repo).announce_revoked(
            "links.acme.com"
        )
        assert ok is True
        cf_client.delete_custom_hostname.assert_awaited_once_with("cf-1")

    async def test_falls_back_to_lookup_when_id_missing(self):
        cf_client = MagicMock()
        cf_client.find_hostname_by_fqdn = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-rescued",
                hostname="links.acme.com",
                status="active",
                ssl_status="active",
            )
        )
        cf_client.delete_custom_hostname = AsyncMock(return_value=True)
        repo = MagicMock()
        doc = MagicMock()
        doc.cf_hostname_id = None
        repo.find_by_fqdn = AsyncMock(return_value=doc)

        ok = await _backend(cf_client=cf_client, repo=repo).announce_revoked(
            "links.acme.com"
        )
        assert ok is True
        cf_client.delete_custom_hostname.assert_awaited_once_with("cf-rescued")

    async def test_already_absent_returns_true(self):
        cf_client = MagicMock()
        cf_client.find_hostname_by_fqdn = AsyncMock(return_value=None)
        repo = MagicMock()
        doc = MagicMock()
        doc.cf_hostname_id = None
        repo.find_by_fqdn = AsyncMock(return_value=doc)
        ok = await _backend(cf_client=cf_client, repo=repo).announce_revoked(
            "links.acme.com"
        )
        assert ok is True
        cf_client.find_hostname_by_fqdn.assert_awaited_once()

    async def test_cf_failure_returns_false_no_raise(self):
        cf_client = MagicMock()
        cf_client.delete_custom_hostname = AsyncMock(
            side_effect=CloudflareAPIError("502")
        )
        repo = MagicMock()
        doc = MagicMock()
        doc.cf_hostname_id = "cf-1"
        repo.find_by_fqdn = AsyncMock(return_value=doc)
        ok = await _backend(cf_client=cf_client, repo=repo).announce_revoked(
            "links.acme.com"
        )
        assert ok is False

    async def test_repo_failure_does_not_raise_falls_back_to_cf_lookup(self):
        # Mongo error during fqdn lookup must not propagate (protocol contract).
        # Backend falls back to CF lookup so a transient DB blip doesn't
        # block revocation when CF still has the record.
        cf_client = MagicMock()
        cf_client.find_hostname_by_fqdn = AsyncMock(
            return_value=CFHostnameResult(
                id="cf-rescued",
                hostname="links.acme.com",
                status="active",
                ssl_status="active",
            )
        )
        cf_client.delete_custom_hostname = AsyncMock(return_value=True)
        repo = MagicMock()
        repo.find_by_fqdn = AsyncMock(side_effect=Exception("mongo down"))
        ok = await _backend(cf_client=cf_client, repo=repo).announce_revoked(
            "links.acme.com"
        )
        assert ok is True
        cf_client.find_hostname_by_fqdn.assert_awaited_once()

    async def test_cf_lookup_failure_returns_false_for_worker_retry(self):
        # When neither doc nor CF lookup resolves a backend_id, return False
        # so eviction_pending stays True and the worker retries — better than
        # silently claiming success and leaving an orphan CF hostname.
        cf_client = MagicMock()
        cf_client.find_hostname_by_fqdn = AsyncMock(
            side_effect=CloudflareAPIError("503 service unavailable")
        )
        repo = MagicMock()
        repo.find_by_fqdn = AsyncMock(return_value=None)
        ok = await _backend(cf_client=cf_client, repo=repo).announce_revoked(
            "links.acme.com"
        )
        assert ok is False
