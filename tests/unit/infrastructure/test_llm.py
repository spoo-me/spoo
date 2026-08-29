"""Unit tests for the LLM capability layer (registry + runner).

All model interaction goes through PydanticAI's TestModel/FunctionModel —
no network, no key. What these pin: the kill switch, schema-validated
output, usage ceilings, typed failure reasons, and prompt versioning."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from config import LlmSettings
from infrastructure.llm import LlmTask, LlmTaskFailed, LlmTaskRunner, build_llm_tasks
from infrastructure.llm.registry import load_prompt


class Verdict(BaseModel):
    label: str
    confidence: str


def _task(**overrides) -> LlmTask:
    defaults = dict(
        name="test-task",
        prompt_version="v1",
        system_prompt="Classify the thing.",
        output_type=Verdict,
    )
    defaults.update(overrides)
    return LlmTask(**defaults)


def _settings(**overrides) -> LlmSettings:
    defaults = dict(enabled=True)
    defaults.update(overrides)
    return LlmSettings(**defaults)


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_disabled_fails_typed_without_touching_a_model(self):
        runner = LlmTaskRunner(_settings(enabled=False), model=TestModel())
        with pytest.raises(LlmTaskFailed) as err:
            await runner.run(_task(), "judge this")
        assert err.value.reason == "disabled"


class TestRun:
    @pytest.mark.asyncio
    async def test_returns_validated_output_instance(self):
        runner = LlmTaskRunner(_settings(), model=TestModel())
        out = await runner.run(_task(), "judge this")
        assert isinstance(out, Verdict)

    @pytest.mark.asyncio
    async def test_usage_ceiling_is_a_typed_failure(self):
        """A runaway tool loop must die as usage_limit, not crash the
        consumer's worker."""

        def loop_forever(messages, info: AgentInfo):
            from pydantic_ai.messages import ModelResponse, ToolCallPart

            # Always call a tool, never finish — the request ceiling has
            # to be what stops it.
            return ModelResponse(
                parts=[ToolCallPart(tool_name="noop", args={})],
            )

        async def noop() -> str:
            return "ok"

        runner = LlmTaskRunner(
            _settings(max_requests_per_run=2), model=FunctionModel(loop_forever)
        )
        with pytest.raises(LlmTaskFailed) as err:
            await runner.run(_task(tools=(noop,)), "judge this")
        assert err.value.reason == "usage_limit"

    @pytest.mark.asyncio
    async def test_task_limits_may_only_tighten_the_globals(self):
        """A task-level request cap below the global one wins."""

        def loop_forever(messages, info: AgentInfo):
            from pydantic_ai.messages import ModelResponse, ToolCallPart

            return ModelResponse(parts=[ToolCallPart(tool_name="noop", args={})])

        async def noop() -> str:
            return "ok"

        runner = LlmTaskRunner(
            _settings(max_requests_per_run=50), model=FunctionModel(loop_forever)
        )
        with pytest.raises(LlmTaskFailed) as err:
            await runner.run(_task(tools=(noop,), max_requests=2), "judge")
        assert err.value.reason == "usage_limit"

    @pytest.mark.asyncio
    async def test_wall_clock_timeout_is_typed(self):
        import asyncio

        async def slow(messages, info: AgentInfo):
            await asyncio.sleep(5)

        runner = LlmTaskRunner(_settings(), model=FunctionModel(slow))
        with pytest.raises(LlmTaskFailed) as err:
            await runner.run(_task(run_timeout_seconds=0.05), "judge")
        assert err.value.reason == "timeout"

    @pytest.mark.asyncio
    async def test_agents_are_cached_per_task(self):
        runner = LlmTaskRunner(_settings(), model=TestModel())
        task = _task()
        await runner.run(task, "one")
        agent = runner._agents[task.name]
        await runner.run(task, "two")
        assert runner._agents[task.name] is agent


class TestVersioning:
    def test_versioned_prompt_carries_declared_version_and_content_hash(self):
        a = _task(system_prompt="alpha")
        b = _task(system_prompt="beta")
        assert a.versioned_prompt.startswith("v1+")
        # Same declared version, different bytes → different stamp: edits
        # that forget to bump the version are still distinguishable.
        assert a.versioned_prompt != b.versioned_prompt


class TestPromptOverride:
    def test_default_when_no_override_dir(self):
        assert load_prompt("t", "default", "") == "default"

    def test_override_file_wins(self, tmp_path):
        (tmp_path / "t.md").write_text("tuned")
        assert load_prompt("t", "default", str(tmp_path)) == "tuned"

    def test_missing_override_file_falls_back(self, tmp_path):
        assert load_prompt("t", "default", str(tmp_path)) == "default"


class TestCatalog:
    def test_duplicate_names_fail_at_build_time(self):
        with pytest.raises(ValueError, match="duplicate"):
            build_llm_tasks([_task(), _task()])

    def test_registry_keys_by_name(self):
        reg = build_llm_tasks([_task()])
        assert set(reg) == {"test-task"}


class TestProviderResolution:
    """One config surface: LLM_API_KEY is threaded into whichever provider
    the model string names — a deploy never also sets ANTHROPIC_API_KEY."""

    def test_anthropic_prefix_builds_an_anthropic_model(self):
        runner = LlmTaskRunner(
            _settings(model="anthropic:claude-sonnet-5", api_key="k")
        )
        model = runner._build_model()
        assert type(model).__name__ == "AnthropicModel"

    def test_openai_prefix_builds_an_openai_model(self):
        runner = LlmTaskRunner(_settings(model="openai:gpt-5-mini", api_key="k"))
        model = runner._build_model()
        assert type(model).__name__ == "OpenAIChatModel"

    def test_without_a_key_the_string_passes_through(self):
        """No LLM_API_KEY = let the provider read its own environment."""
        runner = LlmTaskRunner(_settings(model="anthropic:claude-sonnet-5", api_key=""))
        assert runner._build_model() == "anthropic:claude-sonnet-5"


class TestCostTelemetryIsNotRedacted:
    """Live-run finding: the log redactor's "token" substring heuristic
    ate every cost field, so spend telemetry never reached Axiom."""

    def test_token_count_fields_survive_redaction(self):
        from infrastructure.logging import redact_sensitive_fields

        out = redact_sensitive_fields(
            None,
            "info",
            {
                "event": "llm_task_completed",
                "input_tokens": 11935,
                "output_tokens": 314,
                "cache_read_tokens": 0,
                "api_key": "sk-secret",
            },
        )
        assert out["input_tokens"] == 11935
        assert out["output_tokens"] == 314
        assert out["cache_read_tokens"] == 0
        assert out["api_key"] == "***REDACTED***"  # real secrets still go
