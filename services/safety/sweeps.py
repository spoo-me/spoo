"""L3 sweeps — retroactive coverage. Never iterates links: iterates the
small set (a feed's sync delta, the recent window's distinct hosts) and
uses the dest index into the corpus.

Two sweep kinds ship here:

- **Feed delta** (no schedule of its own): each feed SYNC task calls
  ``FeedDeltaSweeper.sweep`` with the domains new in that sync — existing
  links pointing at a freshly listed domain get their host enqueued for
  analysis within the same task run.
- **Recent screening** (scheduled): distinct destination hosts created in
  the recent window that have no verdict yet get screened, capped per
  run. Unresolved sweep screenings write their verdict silently (the
  analyzer's trigger-aware notification rule), so this never spams the
  operator channel.

Registration follows the feeds discipline: ``build_sweep_tasks`` is the
catalog — a new sweep rule is one factory plus one list entry, and the
scheduler, wiring and worker never change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from infrastructure.logging import get_logger
from repositories.url_repository import UrlRepository
from repositories.verdict_repository import VerdictRepository
from services.safety.events import SafetyAnalyzeEvent
from services.safety.sinks import SafetySink
from services.scheduler.registry import ScheduledTask
from shared.url_utils import registrable_domain

log = get_logger(__name__)

RECENT_SCREEN_TASK = "safety-recent-screen"
_RECENT_SCREEN_CRON = "30 * * * *"
_VERDICT_BATCH = 500


class FeedDeltaSweeper:
    """Fan a feed sync's new domains out to per-host analysis events."""

    def __init__(self, url_repo: UrlRepository, sink: SafetySink) -> None:
        self._url_repo = url_repo
        self._sink = sink

    async def sweep(self, feed: str, new_domains: set[str]) -> int:
        """Returns the number of hosts enqueued. Best-effort per domain —
        one bad lookup never aborts the rest of the delta."""
        enqueued = 0
        for domain in new_domains:
            try:
                hosts = await self._url_repo.list_active_hosts_by_registrable(domain)
            except Exception as exc:
                log.error(
                    "sweep_delta_lookup_failed",
                    domain=domain,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            for host, sample_url in hosts:
                await self._sink.emit(
                    SafetyAnalyzeEvent(
                        url=sample_url,
                        host=host,
                        registrable_domain=domain,
                        trigger="sweep",
                        context={"sweep": "feed_delta", "feed": feed},
                    )
                )
                enqueued += 1
        if enqueued:
            log.warning(
                "sweep_feed_delta_hits",
                feed=feed,
                new_domains=len(new_domains),
                hosts_enqueued=enqueued,
            )
        return enqueued


@dataclass(frozen=True)
class SweepDeps:
    """What the scheduled sweeps need from their host process."""

    url_repo: UrlRepository
    verdict_repo: VerdictRepository
    sink: SafetySink


def recent_screen_task(
    deps: SweepDeps, *, window_hours: int, max_enqueues: int
) -> ScheduledTask:
    """Hourly: screen every never-verdicted destination host created in
    the recent window. Cheap providers only reach these (deep analysis
    admission excludes sweep novelty by default), so the cost per host is
    milliseconds and the output is coverage: every destination on the
    platform ends up with a verdict doc."""

    async def _sweep() -> dict | None:
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        hosts = await deps.url_repo.list_recent_destination_hosts(since)
        judged: set[str] = set()
        host_names = [h for h, _ in hosts]
        for i in range(0, len(host_names), _VERDICT_BATCH):
            judged |= await deps.verdict_repo.hosts_with_verdicts(
                host_names[i : i + _VERDICT_BATCH]
            )
        novel = [(h, url) for h, url in hosts if h not in judged]
        enqueued = 0
        for host, sample_url in novel[:max_enqueues]:
            await deps.sink.emit(
                SafetyAnalyzeEvent(
                    url=sample_url,
                    host=host,
                    registrable_domain=registrable_domain(host),
                    trigger="sweep",
                    context={"sweep": "recent_screen"},
                )
            )
            enqueued += 1
        skipped = max(0, len(novel) - max_enqueues)
        if skipped:
            log.warning("sweep_recent_screen_capped", novel=len(novel), skipped=skipped)
        return {
            "hosts_seen": len(hosts),
            "novel": len(novel),
            "enqueued": enqueued,
        }

    return ScheduledTask(
        name=RECENT_SCREEN_TASK, fn=_sweep, schedule=_RECENT_SCREEN_CRON
    )


def build_sweep_tasks(settings, deps: SweepDeps) -> list[ScheduledTask]:
    """The sweep catalog — one entry per scheduled sweep rule. Feed-delta
    sweeps are not listed here: they ride their feed's sync task."""
    tasks: list[ScheduledTask] = []
    if settings.enabled and settings.sweep_recent_enabled:
        tasks.append(
            recent_screen_task(
                deps,
                window_hours=settings.sweep_recent_window_hours,
                max_enqueues=settings.sweep_max_enqueues,
            )
        )
    return tasks
