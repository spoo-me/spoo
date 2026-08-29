"""LLM task catalog — the capability's consumer registry.

Catalog-as-code, same discipline as ``FEED_REGISTRY`` and
``build_task_registry``: a consumer of the LLM capability is ONE
:class:`LlmTask` declaration, and the runner, retries, cost logging and
kill switch never change when one is added.

Every task is versioned: ``prompt_version`` plus a content hash of the
system prompt is stamped on everything the task produces, so a prompt
change is replayable against past results instead of trusted. Production
prompt text is private tuning — ``LLM_PROMPT_DIR`` overrides the in-repo
default per task (``<task-name>.md``) without a deploy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from infrastructure.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LlmTask:
    """One registered consumer of the LLM capability.

    ``output_type`` is a Pydantic model — the runner returns a validated
    instance, never prose. ``tools`` are plain async callables; the
    runner hands them to the agent as-is (each tool owns its own
    timeboxing and egress rules). Limit fields of ``None`` inherit the
    global ``LlmSettings`` ceilings; a task may only tighten them.
    """

    name: str
    prompt_version: str
    system_prompt: str
    output_type: type
    tools: Sequence[Callable] = field(default_factory=tuple)
    max_requests: int | None = None
    max_tool_calls: int | None = None
    max_total_tokens: int | None = None
    run_timeout_seconds: float | None = None

    @property
    def versioned_prompt(self) -> str:
        """``<version>+<sha8 of the prompt bytes>`` — what gets stamped on
        results. The hash half catches edits that forgot to bump the
        declared version; the declared half stays human-readable."""
        digest = hashlib.sha256(self.system_prompt.encode()).hexdigest()[:8]
        return f"{self.prompt_version}+{digest}"


def load_prompt(task_name: str, default_text: str, prompt_dir: str) -> str:
    """The in-repo default unless ``prompt_dir`` holds ``<task_name>.md``.

    Overrides are read once at build time (tasks are frozen), so a prompt
    edit lands on the next process start — same cadence as env tuning.
    """
    if prompt_dir:
        candidate = Path(prompt_dir) / f"{task_name}.md"
        try:
            text = candidate.read_text()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                "llm_prompt_override_unreadable",
                task=task_name,
                path=str(candidate),
                error=str(exc),
            )
        else:
            log.info("llm_prompt_override_loaded", task=task_name)
            return text
    return default_text


def build_llm_tasks(feature_tasks: Sequence[LlmTask] = ()) -> dict[str, LlmTask]:
    """The catalog. Core tasks (none yet) plus feature-supplied ones —
    mirrors ``build_task_registry``. Duplicate names are a wiring bug and
    fail loudly at startup, not at run time."""
    registry: dict[str, LlmTask] = {}
    for task in feature_tasks:
        if task.name in registry:
            raise ValueError(f"duplicate LLM task name: {task.name}")
        registry[task.name] = task
    return registry
