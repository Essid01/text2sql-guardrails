"""Deterministic detection of known business ambiguities (before any LLM call).

Why a glossary on top of the LLM: known ambiguities ("revenue": gross or net?) are
caught for free, identically every time, and are unit-testable. The LLM can still
ask for clarification on ambiguities the glossary does not know (Phase 2).
"""

from __future__ import annotations

import re
from functools import cached_property
from importlib import resources

import yaml
from pydantic import BaseModel


class ClarificationOption(BaseModel):
    """One possible reading of an ambiguous question."""

    label: str  # short text for a button in the UI
    interpretation: str  # appended to the question once chosen


class Clarification(BaseModel):
    """A structured question back to the user, instead of a guess."""

    term_id: str
    question: str
    options: list[ClarificationOption]


class GlossaryEntry(BaseModel):
    """An ambiguous business term and how to resolve it."""

    id: str
    triggers: list[str]
    resolved_by: list[str]
    question: str
    options: list[ClarificationOption]

    @cached_property
    def _trigger_res(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.triggers]

    @cached_property
    def _resolved_res(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.resolved_by]

    def is_ambiguous(self, question: str) -> bool:
        triggered = any(r.search(question) for r in self._trigger_res)
        return triggered and not any(r.search(question) for r in self._resolved_res)

    def clarification(self) -> Clarification:
        return Clarification(term_id=self.id, question=self.question, options=self.options)


def load_glossary() -> list[GlossaryEntry]:
    """Glossary entries from glossary.yaml."""
    raw = resources.files("text2sql.prompt").joinpath("glossary.yaml").read_text("utf-8")
    return [GlossaryEntry.model_validate(item) for item in yaml.safe_load(raw)]


def detect_ambiguities(question: str, glossary: list[GlossaryEntry]) -> list[Clarification]:
    """All clarifications needed for `question` (empty list = unambiguous for the glossary)."""
    return [entry.clarification() for entry in glossary if entry.is_ambiguous(question)]


def apply_clarification(question: str, option: ClarificationOption) -> str:
    """Rewrite the question with the chosen interpretation, e.g. 'Revenue in March (in 2025)'."""
    return f"{question.strip()} ({option.interpretation})"
