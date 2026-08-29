"""The LLM capability — one client, many consumers.

The model is infrastructure, not a feature: this package owns the
client, budgets, retries, timeouts, the kill switch and cost accounting,
exactly once. Consumers (safety investigation first, the report-inbox
scanner next) register an :class:`LlmTask` — a versioned prompt, a tool
set, an output schema and limits — and get validated objects back from
the runner. Nothing above this layer ever sees a raw completion.
"""

from infrastructure.llm.registry import LlmTask, build_llm_tasks, load_prompt
from infrastructure.llm.runner import LlmTaskFailed, LlmTaskRunner

__all__ = [
    "LlmTask",
    "LlmTaskFailed",
    "LlmTaskRunner",
    "build_llm_tasks",
    "load_prompt",
]
