"""SQL generation: structured LLM output and the generator that produces it."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from text2sql.llm.client import CallUsage, LLMClient
from text2sql.prompt.builder import BuiltPrompt, PromptBuilder

logger = logging.getLogger(__name__)


class ClarificationRequest(BaseModel):
    """A question back to the user when the request is ambiguous."""

    question: str = Field(description="Short question to ask the user.")
    options: list[str] = Field(
        description="2 to 4 possible interpretations the user can choose from."
    )


class SQLGeneration(BaseModel):
    """Structured answer of the SQL generator.

    Field descriptions are sent to the model as part of the JSON schema, so they act
    as instructions. All fields are required (nullable instead of optional): this keeps
    the schema compatible with constrained decoding and forces an explicit choice.
    """

    kind: Literal["sql", "clarification", "refusal"] = Field(
        description="'sql' if the question can be answered with one query; 'clarification' "
        "if it is ambiguous in a way that changes the result; 'refusal' if it cannot be "
        "answered with this schema or asks to modify data."
    )
    explanation: str = Field(
        description="One or two sentences for a business user: what the query computes "
        "and the assumptions made (statuses counted, gross or net, period)."
    )
    sql: str | None = Field(
        description="A single PostgreSQL SELECT (or WITH ... SELECT) statement when kind "
        "is 'sql', otherwise null."
    )
    confidence: float = Field(
        description="Your confidence, from 0 to 1, that the query returns exactly what was asked."
    )
    tables_used: list[str] = Field(description="Tables referenced by the query.")
    columns_used: list[str] = Field(description="Columns referenced by the query, as table.column.")
    clarification: ClarificationRequest | None = Field(
        description="Required when kind is 'clarification', otherwise null."
    )
    refusal_reason: str | None = Field(
        description="Required when kind is 'refusal': what is missing or not allowed. "
        "Otherwise null."
    )

    @model_validator(mode="after")
    def _consistent(self) -> SQLGeneration:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.kind == "sql":
            if not (self.sql and self.sql.strip()):
                raise ValueError("kind='sql' requires a non-empty sql")
            if self.clarification is not None or self.refusal_reason is not None:
                raise ValueError("kind='sql' must not carry a clarification or refusal")
        elif self.kind == "clarification":
            if self.clarification is None or len(self.clarification.options) < 2:
                raise ValueError("kind='clarification' requires a question and >= 2 options")
            if self.sql is not None:
                raise ValueError("kind='clarification' must not carry sql")
        elif self.kind == "refusal":
            if not (self.refusal_reason and self.refusal_reason.strip()):
                raise ValueError("kind='refusal' requires a refusal_reason")
            if self.sql is not None:
                raise ValueError("kind='refusal' must not carry sql")
        return self


class GenerationResult(BaseModel):
    """Generator output plus everything needed for logs, audit and evaluation."""

    output: SQLGeneration
    prompt: BuiltPrompt
    usage: CallUsage


class SQLGenerator:
    """Question -> prompt -> LLM -> validated SQLGeneration."""

    def __init__(self, builder: PromptBuilder, llm: LLMClient) -> None:
        self.builder = builder
        self.llm = llm

    def generate(self, question: str) -> GenerationResult:
        prompt = self.builder.build(question)
        output, usage = self.llm.complete(prompt.system, prompt.user, SQLGeneration)
        logger.info(
            "sql generated kind=%s confidence=%.2f tokens_in=%d tokens_out=%d cached=%s",
            output.kind, output.confidence, usage.input_tokens, usage.output_tokens,
            usage.cached,
        )  # fmt: skip
        return GenerationResult(output=output, prompt=prompt, usage=usage)
