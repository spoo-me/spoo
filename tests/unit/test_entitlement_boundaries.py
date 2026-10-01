"""Import lint: nothing outside the entitlements package reads the plan
stores for gating.

Routes and services decide from the resolved map the ``Entitled``
dependency hands them. The only modules allowed to touch ``subscriptions``,
``entitlement_overrides`` or ``feature_flags`` are the resolver, the flag
evaluator, the composition root, and the erasure cascade (which deletes).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = (
    re.compile(r"repositories\.subscription_repository"),
    re.compile(r"repositories\.entitlement_override_repository"),
    re.compile(r"repositories\.feature_flag_repository"),
    re.compile(r'db\["(subscriptions|entitlement_overrides|feature_flags)"\]'),
)

ALLOWED = {
    "dependencies/wiring.py",
    "services/account_erasure_service.py",
    "services/feature_flag_service.py",
}
ALLOWED_PREFIXES = ("services/entitlements/", "repositories/")


def _python_files():
    for folder in ("routes", "services", "dependencies", "middleware", "workers"):
        yield from (ROOT / folder).rglob("*.py")


def test_no_gating_code_reads_the_plan_stores_directly():
    offenders = []
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or rel.startswith(ALLOWED_PREFIXES):
            continue
        text = path.read_text()
        for pattern in FORBIDDEN:
            if pattern.search(text):
                offenders.append(f"{rel}: {pattern.pattern}")
    assert offenders == [], "\n".join(offenders)


def test_nothing_reads_the_deleted_users_plan():
    offenders = []
    for path in _python_files():
        text = path.read_text()
        if re.search(r"\buser(_doc)?\.plan\b|UserPlan\b|\.tier\b", text):
            offenders.append(path.relative_to(ROOT).as_posix())
    # Safety verdicts have their own ``tier`` field; nothing else may.
    offenders = [o for o in offenders if not o.startswith("services/safety/")]
    assert offenders == [], "\n".join(offenders)
