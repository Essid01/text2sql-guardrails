"""Evaluation cases: hand-written questions with reference SQL (evals/questions.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

Expect = Literal["answer", "clarification", "no_answer", "unsafe"]
# Repository root (editable install): src/text2sql/eval/dataset.py -> parents[3].
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "evals" / "questions.yaml"


class EvalCase(BaseModel):
    id: str
    category: str
    lang: Literal["en", "fr"]
    expect: Expect
    question: str
    gold_sql: str | None = None
    alt_gold_sql: list[str] = []
    bad_sql: str | None = None
    bad_kind: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> EvalCase:
        if (self.expect == "answer") != (self.gold_sql is not None):
            raise ValueError(f"{self.id}: gold_sql is required for, and only for, answers")
        if (self.bad_sql is None) != (self.bad_kind is None):
            raise ValueError(f"{self.id}: bad_sql and bad_kind go together")
        if self.bad_sql is not None and self.expect != "answer":
            raise ValueError(f"{self.id}: bad_sql only makes sense for answers")
        return self

    @property
    def golds(self) -> list[str]:
        return [self.gold_sql, *self.alt_gold_sql] if self.gold_sql else []


def load_cases(path: Path = DEFAULT_PATH) -> list[EvalCase]:
    cases = [EvalCase.model_validate(c) for c in yaml.safe_load(path.read_text("utf-8"))]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases
