"""Repository for the ``safety_verdicts`` collection.

Verdicts are upserted by host: re-analysis refreshes the existing doc
rather than accumulating history (run history lives in logs). Reads are
point lookups on the unique host index.
"""

from __future__ import annotations

from datetime import datetime, timezone

from repositories.base import BaseRepository
from schemas.enums.safety import VerdictTier
from schemas.models.verdict import VerdictDoc


class VerdictRepository(BaseRepository[VerdictDoc]):
    async def upsert_verdict(
        self,
        host: str,
        *,
        registrable_domain: str,
        tier: VerdictTier,
        reason: str | None,
        source: str,
        trigger: str,
        sample_url: str | None = None,
        context: dict | None = None,
        decided_by: str = "system",
        scope: str | None = None,
        path_pattern: str | None = None,
        provenance: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        fields = {
            "registrable_domain": registrable_domain,
            "tier": tier.value,
            "reason": reason,
            "source": source,
            "trigger": trigger,
            "sample_url": sample_url,
            "context": context,
            "decided_by": decided_by,
            "updated_at": now,
        }
        # A re-verdict saying nothing about scope must not erase (or resurrect)
        # an earlier bound, so absent means untouched.
        if scope is not None:
            fields["scope"] = scope
            fields["path_pattern"] = path_pattern
        # Deep-tier provenance (model, prompt_version, classification,
        # confidence, evidence, egress, corroborated) — merged only when an
        # investigation produced this verdict, so screening writes stay
        # untouched.
        if provenance:
            fields.update(provenance)
        await self._col.update_one(
            {"host": host},
            {"$set": fields, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def find_by_host(self, host: str) -> VerdictDoc | None:
        return await self._find_one({"host": host})

    async def hosts_with_verdicts(self, hosts: list[str]) -> set[str]:
        """Which of *hosts* already have a verdict — the screening sweep's
        novelty filter, one $in query on the unique host index."""
        if not hosts:
            return set()
        docs = await self._col.find({"host": {"$in": hosts}}, {"host": 1}).to_list(
            length=len(hosts)
        )
        return {d["host"] for d in docs}
