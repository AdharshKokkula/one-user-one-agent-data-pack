"""
Model Provider – Step 6

Abstraction for model calls with:
  - MockProvider (default, deterministic, no API key)
  - Replays faults from model_faults.jsonl (timeout, broken JSON, unsupported claims, down)
  - Safe JSON parsing with bounded retries
  - Rule-based fallback on failure
  - Configurable model/tool call budgets
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import config as cfg


@dataclass
class ModelCallResult:
    """Result of a model call."""
    success: bool
    output: dict | None = None
    error: str | None = None
    latency_ms: int = 0
    fault_type: str | None = None
    retries: int = 0


@dataclass
class CallBudget:
    """Tracks model and tool call budgets."""
    max_model_calls: int = cfg.MAX_MODEL_CALLS
    max_tool_calls: int = cfg.MAX_TOOL_CALLS
    model_calls_used: int = 0
    tool_calls_used: int = 0

    def can_call_model(self) -> bool:
        return self.model_calls_used < self.max_model_calls

    def can_call_tool(self) -> bool:
        return self.tool_calls_used < self.max_tool_calls

    def record_model_call(self) -> None:
        self.model_calls_used += 1

    def record_tool_call(self) -> None:
        self.tool_calls_used += 1


class ModelProvider(ABC):
    """Abstract base for model providers."""

    @abstractmethod
    def call(self, prompt: str, context: dict | None = None) -> ModelCallResult:
        """Make a model call. Returns ModelCallResult."""
        pass

    @abstractmethod
    def name(self) -> str:
        pass


class MockProvider(ModelProvider):
    """Deterministic mock provider that never needs an API key.

    Replays faults from model_faults.jsonl when the trigger_event_id matches.
    Otherwise returns a simple deterministic response.
    """

    def __init__(self, model_faults: dict[str, dict] | None = None):
        self._faults = model_faults or {}
        self._current_trigger_id: str | None = None
        self._control_events: list[dict] = []
        self._current_time: Any = None

    def set_trigger(self, trigger_event_id: str) -> None:
        """Set the current trigger for fault replay."""
        self._current_trigger_id = trigger_event_id

    def set_control_events(self, events: list[dict]) -> None:
        """Set control events for dependency simulation."""
        self._control_events = events

    def set_current_time(self, dt: Any) -> None:
        """Set current simulation time."""
        self._current_time = dt

    def name(self) -> str:
        return "mock"

    def call(self, prompt: str, context: dict | None = None) -> ModelCallResult:
        """Mock model call with fault replay."""
        start = time.monotonic()

        # Check for dependency unavailability
        if self._current_time and self._control_events:
            for ce in self._control_events:
                if ce.get("type") == "dependency_unavailable" and ce.get("dependency") == "model_provider":
                    from .loader import _ts
                    unavail_start = ce.get("_at")
                    unavail_end = _ts(ce.get("until"))
                    if unavail_start and unavail_end and unavail_start <= self._current_time <= unavail_end:
                        return ModelCallResult(
                            success=False,
                            error="model_provider unavailable (control event)",
                            latency_ms=100,
                            fault_type="dependency_unavailable",
                        )

        # Check for scripted faults
        if self._current_trigger_id and self._current_trigger_id in self._faults:
            fault = self._faults[self._current_trigger_id]
            fault_type = fault.get("fault")

            if fault_type == "timeout":
                return ModelCallResult(
                    success=False,
                    error=f"Timeout after {fault.get('seconds', 45)}s",
                    latency_ms=int(fault.get("seconds", 45) * 1000),
                    fault_type="timeout",
                )

            if fault_type == "malformed_output":
                return ModelCallResult(
                    success=False,
                    error=f"Malformed JSON: {fault.get('model_output', '')}",
                    latency_ms=int((time.monotonic() - start) * 1000),
                    fault_type="malformed_output",
                )

            if fault_type == "unsupported_claim":
                # Return the model's output but flag it as unsupported
                return ModelCallResult(
                    success=True,
                    output=fault.get("model_output"),
                    latency_ms=int((time.monotonic() - start) * 1000),
                    fault_type="unsupported_claim",
                )

        # Default mock response: just acknowledge
        latency = int((time.monotonic() - start) * 1000)
        return ModelCallResult(
            success=True,
            output={"status": "ok", "mock": True},
            latency_ms=max(latency, 1),
        )


def safe_json_parse(raw: str) -> dict | None:
    """Safely parse JSON from model output. Returns None on failure."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Try to extract JSON from markdown code blocks
        if "```json" in raw:
            start = raw.index("```json") + 7
            end = raw.index("```", start) if "```" in raw[start:] else len(raw)
            try:
                return json.loads(raw[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass
        return None


def call_with_retries(
    provider: ModelProvider,
    prompt: str,
    budget: CallBudget,
    context: dict | None = None,
    max_retries: int = cfg.MODEL_MAX_RETRIES,
) -> ModelCallResult:
    """Call the model with bounded retries, respecting budget.

    Returns the result of the last attempt.
    """
    last_result = None
    for attempt in range(max_retries + 1):
        if not budget.can_call_model():
            return ModelCallResult(
                success=False,
                error="Model call budget exhausted",
                fault_type="budget_exhausted",
            )

        budget.record_model_call()
        result = provider.call(prompt, context)
        last_result = result
        result.retries = attempt

        if result.success:
            return result

        # Don't retry timeouts or budget exhaustion
        if result.fault_type in ("timeout", "budget_exhausted"):
            return result

    return last_result or ModelCallResult(
        success=False,
        error="All retries exhausted",
        fault_type="retries_exhausted",
    )
