"""LLM clients returning validated Pydantic objects, with per-call token and cost usage."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import pydantic
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The LLM call failed (network, API error, unusable output)."""


class LLMRefusalError(LLMError):
    """The model declined the request (safety classifier, stop_reason == 'refusal')."""


class LLMOutputError(LLMError):
    """The model answered but the output is truncated or fails validation."""


class CallUsage(BaseModel):
    """Tokens and cost of one logical call (validation retries included)."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    api_calls: int = 0  # real HTTP calls made (0 on a cache hit)
    cached: bool = False
    duration_ms: float = 0.0


class UsageTracker:
    """Accumulates usage over several calls (one question, or a whole evaluation run)."""

    def __init__(self) -> None:
        self.calls: list[CallUsage] = []

    def add(self, usage: CallUsage) -> None:
        self.calls.append(usage)

    @property
    def total(self) -> dict[str, float]:
        return {
            "llm_calls": len(self.calls),
            "api_calls": sum(c.api_calls for c in self.calls),
            "cache_hits": sum(c.cached for c in self.calls),
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "cost_usd": round(sum(c.cost_usd for c in self.calls), 6),
        }


class LLMClient(Protocol):
    """Provider-agnostic interface: (system, user) -> validated `response_model` instance."""

    @property
    def fingerprint(self) -> dict[str, Any]:
        """Parameters that change the output (used in cache keys)."""
        ...

    def complete(self, system: str, user: str, response_model: type[T]) -> tuple[T, CallUsage]:
        """Return the parsed output and the usage of the call."""
        ...


def _wrap_sdk_error(exc: Exception) -> LLMError | None:
    """Translate Anthropic SDK errors into LLMError; None for anything else."""
    import anthropic

    if isinstance(exc, anthropic.APIStatusError):
        return LLMError(f"Anthropic API error {exc.status_code}: {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError(f"cannot reach Anthropic API: {exc}")
    return None


class AnthropicClient:
    """Claude via the official SDK with native structured outputs (`messages.parse`).

    Measured on claude-haiku-5-5: native structured outputs guarantee schema-valid JSON
    and cost ~40% fewer input tokens than a tool-based approach (no tool definition).
    Cross-field rules (Pydantic validators) are checked after parsing; on failure the
    error is sent back once so the model can fix its answer.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        effort: str = "low",
        max_tokens: int = 4096,
        price_input_per_mtok: float = 0.0,
        price_output_per_mtok: float = 0.0,
        max_validation_retries: int = 1,
        sdk_client: Any | None = None,  # injectable for tests
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.price_in = price_input_per_mtok
        self.price_out = price_output_per_mtok
        self.max_validation_retries = max_validation_retries
        if sdk_client is None:
            import anthropic  # lazy: the SDK takes seconds to import (thousands of type modules)

            # The SDK already retries 429/5xx/connection errors with backoff (2 retries).
            sdk_client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
        self._client = sdk_client

    @property
    def fingerprint(self) -> dict[str, Any]:
        return {"provider": "anthropic", "model": self.model, "effort": self.effort,
                "max_tokens": self.max_tokens}  # fmt: skip

    def complete(self, system: str, user: str, response_model: type[T]) -> tuple[T, CallUsage]:
        usage = CallUsage(model=self.model)
        start = time.perf_counter()
        content = user
        for attempt in range(self.max_validation_retries + 1):
            try:
                response = self._client.messages.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                    output_format=response_model,
                    output_config={"effort": self.effort},
                )
            except pydantic.ValidationError as exc:
                # Schema-valid JSON that breaks a cross-field rule. Tokens of this attempt
                # are not reported by the SDK on this path; the retry is counted as a call.
                usage.api_calls += 1
                if attempt == self.max_validation_retries:
                    raise LLMOutputError(f"invalid output after retry: {exc}") from exc
                logger.warning("llm output failed validation, retrying: %s", exc)
                content = (
                    f"{user}\n\nYour previous answer was rejected by validation:\n{exc}\n"
                    "Return a corrected answer."
                )
                continue
            except Exception as exc:
                wrapped = _wrap_sdk_error(exc)
                if wrapped is None:
                    raise
                raise wrapped from exc

            usage.api_calls += 1
            self._add_tokens(usage, response.usage)
            if response.stop_reason == "refusal":
                raise LLMRefusalError("the model declined to answer this request")
            if response.stop_reason == "max_tokens":
                raise LLMOutputError("output truncated (max_tokens reached)")
            if response.parsed_output is None:
                raise LLMOutputError("no structured output in the response")
            usage.duration_ms = round((time.perf_counter() - start) * 1000, 1)
            return response.parsed_output, usage

        raise AssertionError("unreachable")  # pragma: no cover

    def _add_tokens(self, usage: CallUsage, api_usage: Any) -> None:
        usage.input_tokens += api_usage.input_tokens
        usage.output_tokens += api_usage.output_tokens
        usage.cost_usd = round(
            (usage.input_tokens * self.price_in + usage.output_tokens * self.price_out) / 1_000_000,
            8,
        )


class FakeLLMClient:
    """Scripted LLM for tests and CI: no network, no key, no cost.

    `responder` receives (system, user, response_model) and returns the output object.
    Every call is recorded in `self.calls`.
    """

    def __init__(self, responder: Callable[[str, str, type[BaseModel]], BaseModel]) -> None:
        self._responder = responder
        self.calls: list[tuple[str, str]] = []

    @property
    def fingerprint(self) -> dict[str, Any]:
        return {"provider": "fake"}

    def complete(self, system: str, user: str, response_model: type[T]) -> tuple[T, CallUsage]:
        self.calls.append((system, user))
        output = self._responder(system, user, response_model)
        if not isinstance(output, response_model):
            raise LLMOutputError(f"fake returned {type(output).__name__}")
        return output, CallUsage(model="fake", api_calls=1)
