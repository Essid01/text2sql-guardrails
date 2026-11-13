"""Back-translation: SQL -> question (blind), then judge it against the user's question.

Two separate calls, on purpose:
1. The translator sees the SQL and the schema, NOT the original question. If it saw the
   question it would tend to rationalize the SQL instead of reading it.
2. The judge sees both questions, NOT the SQL. It compares meanings (period, metric,
   statuses, grouping, ranking) rather than re-reviewing code.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from text2sql.llm.client import CallUsage, LLMClient
from text2sql.prompt.schema_render import render_schema
from text2sql.schema.models import DatabaseSchema

TRANSLATOR_SYSTEM = """\
You review PostgreSQL queries written for a retail sales database. Read the query and
write the business question it answers, as a user would ask it.

Describe what the query actually does, not what someone probably wanted: include every
filter (statuses, date range), the exact metric (for example net revenue after discount
vs gross revenue), the grouping level, the ordering direction and any row limit. If the
query looks wrong or odd, still describe it literally."""

JUDGE_SYSTEM = """\
You check whether a SQL query answers a user's question. You get the user's question (A)
and a literal description of what the query computes (B). Decide whether B answers A.

Count as a mismatch anything that changes the result: different period or year, gross
instead of net (or the reverse), different statuses included, different grouping level
or entity, wrong ranking direction, different number of rows asked, missing or extra
filter.

Do not count as a mismatch: wording differences, or B being more precise than A in a way
that follows this database's conventions (sales and revenue count only 'completed'
orders; amounts in EUR; dates are order dates)."""


class BackTranslation(BaseModel):
    question: str = Field(description="The business question the query answers, precisely.")


class AlignmentJudgement(BaseModel):
    differences: list[str] = Field(
        description="Each difference between A and B that changes the result. Empty if none."
    )
    aligned: bool = Field(description="True if B answers A.")
    score: float = Field(
        description="1.0 = B answers exactly A; 0.5 = partially; 0.0 = something else."
    )

    @model_validator(mode="after")
    def _score_in_range(self) -> AlignmentJudgement:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        return self


class BackTranslationResult(BaseModel):
    reconstructed_question: str
    judgement: AlignmentJudgement
    usages: list[CallUsage]


class BackTranslator:
    """Runs the two calls. Uses only the tables of the query in the schema (fewer tokens)."""

    def __init__(self, llm: LLMClient, schema: DatabaseSchema) -> None:
        self.llm = llm
        self.schema = schema

    def check(self, question: str, sql: str, tables: set[str]) -> BackTranslationResult:
        known = tables & set(self.schema.table_names)
        schema_text = render_schema(self.schema.subset(known or set(self.schema.table_names)))
        translation, u1 = self.llm.complete(
            TRANSLATOR_SYSTEM,
            f"<schema>\n{schema_text}\n</schema>\n\n<query>\n{sql.strip()}\n</query>",
            BackTranslation,
        )
        judgement, u2 = self.llm.complete(
            JUDGE_SYSTEM,
            f"<question_a>\n{question.strip()}\n</question_a>\n\n"
            f"<question_b>\n{translation.question.strip()}\n</question_b>",
            AlignmentJudgement,
        )
        return BackTranslationResult(
            reconstructed_question=translation.question, judgement=judgement, usages=[u1, u2]
        )
