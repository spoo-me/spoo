"""LLM task runner — the only place that talks to a model.

Wraps a PydanticAI agent per registered task: hard usage ceilings, a
wall-clock timeout, typed failure, and cost accounting into structlog
(tokens and dollars land as fields, so Axiom answers "what did we spend
on what" with no second system).

Failure is a value here: every way a run can die — kill switch off,
budget ceiling, model/API error, timeout, schema exhaustion — surfaces
as :class:`LlmTaskFailed` with a machine ``reason``, because consumers
like the safety analyzer must degrade (verdict stays uncertain, review
queue pinged) rather than crash their worker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.usage import UsageLimits

from config import LlmSettings
from infrastructure.llm.registry import LlmTask
from infrastructure.logging import get_logger

log = get_logger(__name__)


class LlmTaskFailed(Exception):
    """A task run that produced no validated output. ``reason`` is
    machine-readable: disabled | usage_limit | timeout | model_error |
    invalid_output."""

    def __init__(self, task: str, reason: str, detail: str = "") -> None:
        self.task = task
        self.reason = reason
        self.detail = detail
        super().__init__(f"llm task {task} failed: {reason} {detail}".strip())


class LlmTaskRunner:
    """One runner per process; agents are built lazily and cached per
    task (PydanticAI agents are stateless across runs)."""

    def __init__(self, settings: LlmSettings, *, model: Any | None = None) -> None:
        self._settings = settings
        self._model = model  # tests inject TestModel; prod builds from settings
        self._agents: dict[str, Agent] = {}

    def _build_model(self) -> Any:
        """Resolve ``<provider>:<model>`` into a PydanticAI model.

        The explicit key from ``LLM_API_KEY`` is threaded into the
        provider so the capability has exactly ONE config surface — a
        deploy never has to also set ANTHROPIC_API_KEY / OPENAI_API_KEY.
        Without a key we hand the string back and let the provider read
        its own environment.
        """
        if self._model is not None:
            return self._model
        model_id = self._settings.model
        key = self._settings.api_key
        if key and model_id.startswith("anthropic:"):
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(
                model_id.removeprefix("anthropic:"),
                provider=AnthropicProvider(api_key=key),
            )
        if key and model_id.startswith("openai:"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            return OpenAIChatModel(
                model_id.removeprefix("openai:"),
                provider=OpenAIProvider(api_key=key),
            )
        return model_id  # provider infers credentials from its environment

    def _agent(self, task: LlmTask) -> Agent:
        agent = self._agents.get(task.name)
        if agent is None:
            model_settings: dict = {"timeout": self._settings.request_timeout_seconds}
            if self._settings.model.startswith("anthropic:"):
                # Every tool round resends the whole conversation; the moving
                # breakpoint cuts loop input cost ~3-4x. Other providers
                # ignore anthropic_* keys.
                model_settings.update(
                    anthropic_cache=True,
                    anthropic_cache_instructions=True,
                    anthropic_cache_tool_definitions=True,
                )
            agent = Agent(
                self._build_model(),
                instructions=task.system_prompt,
                output_type=task.output_type,
                tools=list(task.tools),
                name=task.name,
                model_settings=model_settings,
            )
            self._agents[task.name] = agent
        return agent

    async def run(self, task: LlmTask, prompt: str) -> Any:
        """Run one task to a validated ``task.output_type`` instance."""
        if not self._settings.enabled:
            raise LlmTaskFailed(task.name, "disabled")
        # Three independent ceilings, because they fail differently: a
        # runaway loop trips requests, a chatty page trips tokens, and a
        # model re-calling one tool trips tool_calls.
        limits = UsageLimits(
            request_limit=task.max_requests or self._settings.max_requests_per_run,
            tool_calls_limit=(
                task.max_tool_calls or self._settings.max_tool_calls_per_run
            ),
            total_tokens_limit=(
                task.max_total_tokens or self._settings.max_total_tokens_per_run
            ),
        )
        timeout = task.run_timeout_seconds or self._settings.run_timeout_seconds
        started = time.perf_counter()
        try:
            # wait_for, not asyncio.timeout: 3.10 support (see safe_fetch).
            result = await asyncio.wait_for(
                self._agent(task).run(prompt, usage_limits=limits), timeout=timeout
            )
        except UsageLimitExceeded as exc:
            self._log_failure(task, "usage_limit", exc, started)
            raise LlmTaskFailed(task.name, "usage_limit", str(exc)) from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self._log_failure(task, "timeout", exc, started)
            raise LlmTaskFailed(task.name, "timeout", f"{timeout}s") from exc
        except ModelAPIError as exc:
            self._log_failure(task, "model_error", exc, started)
            raise LlmTaskFailed(task.name, "model_error", str(exc)) from exc
        except UnexpectedModelBehavior as exc:
            # Retries exhausted on schema validation, empty responses, etc.
            self._log_failure(task, "invalid_output", exc, started)
            raise LlmTaskFailed(task.name, "invalid_output", str(exc)) from exc

        usage = result.usage
        log.info(
            "llm_task_completed",
            task=task.name,
            model=str(self._settings.model),
            prompt_version=task.versioned_prompt,
            requests=usage.requests,
            tool_calls=usage.tool_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return result.output

    def _log_failure(
        self, task: LlmTask, reason: str, exc: Exception, started: float
    ) -> None:
        log.warning(
            "llm_task_failed",
            task=task.name,
            model=str(self._settings.model),
            prompt_version=task.versioned_prompt,
            reason=reason,
            error=str(exc),
            error_type=type(exc).__name__,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
