"""Document model for the ``safety_verdicts`` collection.

One verdict per destination HOST — many short links to one scam host share
a single judgment, computed once by whichever detection layer got there
first and enforced by reading, never recomputing, at the edges. Host
granularity (not registrable domain) keeps shared platforms (workers.dev,
free hosts) from being collectively punished; domain-wide action stays a
deliberate operator move.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.enums.safety import VerdictTier
from schemas.models.base import MongoBaseModel


class VerdictDoc(MongoBaseModel):
    host: str
    registrable_domain: str = ""
    # One example URL that led to this verdict, for review context.
    sample_url: str | None = None
    tier: VerdictTier
    # Human-readable reason string ("matched blocklist pattern …"); doubles
    # as the audit trail for appeals.
    reason: str | None = None
    # Which engine produced it: "local_feeds" now, "llm"/"human" later.
    source: str = "local_feeds"
    # What put the destination in front of the engine: "report" now,
    # "hot"/"pattern"/"sweep"/"edit" later.
    trigger: str = "report"
    # Trigger-specific snapshot (report reasons, counts) for review embeds.
    context: dict | None = None
    decided_by: str = "system"
    # Deep-tier (investigation) provenance — null on screening verdicts.
    # ``model`` + ``prompt_version`` make a verdict replayable against a
    # later prompt; ``evidence`` is what the model rested on; ``egress``
    # is which IP the render came from (a clean render from a scanner IP
    # is weaker than a clean render); ``corroborated`` records whether a
    # hard external source agreed, which is what the authority mapper
    # gates auto-block on.
    model: str | None = None
    prompt_version: str | None = None
    classification: str | None = None
    confidence: str | None = None
    evidence: list[str] | None = None
    egress: str | None = None
    corroborated: bool | None = None
    # The scope decision and its reasoning — a host block is the most
    # destructive action here, so why it was chosen is auditable.
    scope: str | None = None
    path_pattern: str | None = None
    scope_justification: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = Field(default=None)
