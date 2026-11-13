"""Unit tests for LLM clients and cache (SDK mocked: no network, no key)."""

from types import SimpleNamespace
from typing import Any

import pydantic
import pytest
from pydantic import BaseModel, model_validator

from text2sql.llm import (
    AnthropicClient,
    CallUsage,
    FakeLLMClient,
    LLMOutputError,
    LLMRefusalError,
    UsageTracker,
)
from text2sql.llm.cache import CachedLLMClient, cache_key


class Answer(BaseModel):
    sql: str


class StrictAnswer(BaseModel):
    sql: str

    @model_validator(mode="after")
    def must_select(self) -> "StrictAnswer":
        if not self.sql.startswith("SELECT"):
            raise ValueError("sql must start with SELECT")
        return self


def _response(parsed: Any, stop: str = "end_turn", tin: int = 100, tout: int = 20):
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
    )


class FakeSDK:
    """Mimics `anthropic.Anthropic().messages.parse` with scripted outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(sdk: FakeSDK, **kwargs: Any) -> AnthropicClient:
    return AnthropicClient(api_key="x", model="claude-haiku-5-5", sdk_client=sdk,
                           price_input_per_mtok=0.10, price_output_per_mtok=0.50,
                           **kwargs)  # fmt: skip


def _validation_error() -> pydantic.ValidationError:
    try:
        StrictAnswer(sql="DROP TABLE x")
    except pydantic.ValidationError as exc:
        return exc
    raise AssertionError


def test_success_returns_output_and_usage() -> None:
    sdk = FakeSDK([_response(Answer(sql="SELECT 1"), tin=1000, tout=200)])
    out, usage = _client(sdk).complete("sys", "user", Answer)
    assert out == Answer(sql="SELECT 1")
    assert (usage.input_tokens, usage.output_tokens, usage.api_calls) == (1000, 200, 1)
    assert usage.cost_usd == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)


def test_request_respects_model_constraints() -> None:
    """Haiku 5.5 rejects sampling params: we must never send them."""
    sdk = FakeSDK([_response(Answer(sql="SELECT 1"))])
    _client(sdk, effort="low", max_tokens=4096).complete("sys", "question", Answer)
    req = sdk.requests[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["system"] == "sys"
    assert req["messages"] == [{"role": "user", "content": "question"}]  # no prefill
    assert req["output_config"] == {"effort": "low"}
    assert req["output_format"] is Answer
    assert not {"temperature", "top_p", "top_k", "thinking"} & req.keys()


def test_refusal_raises() -> None:
    sdk = FakeSDK([_response(None, stop="refusal")])
    with pytest.raises(LLMRefusalError):
        _client(sdk).complete("s", "u", Answer)


def test_truncated_output_raises() -> None:
    sdk = FakeSDK([_response(None, stop="max_tokens")])
    with pytest.raises(LLMOutputError, match="truncated"):
        _client(sdk).complete("s", "u", Answer)


def test_validation_error_is_retried_once_with_feedback() -> None:
    sdk = FakeSDK([_validation_error(), _response(StrictAnswer(sql="SELECT 1"))])
    out, usage = _client(sdk).complete("s", "question", StrictAnswer)
    assert out.sql == "SELECT 1"
    assert usage.api_calls == 2
    retry_content = sdk.requests[1]["messages"][0]["content"]
    assert retry_content.startswith("question") and "rejected by validation" in retry_content


def test_validation_error_twice_raises() -> None:
    sdk = FakeSDK([_validation_error(), _validation_error()])
    with pytest.raises(LLMOutputError, match="after retry"):
        _client(sdk).complete("s", "u", StrictAnswer)


def test_fake_client_records_calls() -> None:
    fake = FakeLLMClient(lambda s, u, m: Answer(sql=f"SELECT '{u}'"))
    out, usage = fake.complete("sys", "hi", Answer)
    assert out.sql == "SELECT 'hi'" and fake.calls == [("sys", "hi")]
    assert usage.cost_usd == 0


def test_cache_hit_costs_nothing_and_persists(tmp_path) -> None:
    counter = {"n": 0}

    def responder(s: str, u: str, m: type) -> Answer:
        counter["n"] += 1
        return Answer(sql="SELECT 1")

    path = tmp_path / "cache.sqlite"
    first, u1 = CachedLLMClient(FakeLLMClient(responder), path).complete("s", "q", Answer)
    # New instance, same file: simulates a new process.
    second, u2 = CachedLLMClient(FakeLLMClient(responder), path).complete("s", "q", Answer)
    assert first == second
    assert counter["n"] == 1
    assert (u1.cached, u2.cached) == (False, True)
    assert (u2.cost_usd, u2.api_calls) == (0.0, 0)


def test_cache_key_changes_with_every_input() -> None:
    base = cache_key({"model": "a"}, "sys", "user", {"x": 1})
    assert cache_key({"model": "b"}, "sys", "user", {"x": 1}) != base
    assert cache_key({"model": "a"}, "sys2", "user", {"x": 1}) != base
    assert cache_key({"model": "a"}, "sys", "user2", {"x": 1}) != base
    assert cache_key({"model": "a"}, "sys", "user", {"x": 2}) != base
    assert cache_key({"model": "a"}, "sys", "user", {"x": 1}) == base


def test_usage_tracker_totals() -> None:
    tracker = UsageTracker()
    tracker.add(CallUsage(model="m", input_tokens=10, output_tokens=5, cost_usd=0.001, api_calls=1))
    tracker.add(CallUsage(model="m", input_tokens=10, output_tokens=5, cached=True))
    total = tracker.total
    assert (total["llm_calls"], total["api_calls"], total["cache_hits"]) == (2, 1, 1)
    assert (total["input_tokens"], total["output_tokens"]) == (20, 10)
    assert total["cost_usd"] == 0.001
