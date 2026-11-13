"""Runs every enabled check on an executed query and builds the confidence report."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from text2sql.execution import QueryResult
from text2sql.generation import SQLGeneration
from text2sql.guardrails.sql_guard import referenced_tables
from text2sql.llm.client import CallUsage, LLMError
from text2sql.schema.models import DatabaseSchema
from text2sql.verification.backtranslation import BackTranslationResult, BackTranslator
from text2sql.verification.confidence import ConfidenceReport, compute_confidence
from text2sql.verification.consensus import ConsensusChecker, ConsensusResult
from text2sql.verification.sanity import Finding, run_sanity_checks

logger = logging.getLogger(__name__)


class VerificationReport(BaseModel):
    findings: list[Finding]
    backtranslation: BackTranslationResult | None = None
    consensus: ConsensusResult | None = None
    confidence: ConfidenceReport
    usages: list[CallUsage] = []


class Verifier:
    """Sanity checks always run (free); LLM-based checks are optional (cost)."""

    def __init__(
        self,
        schema: DatabaseSchema,
        backtranslator: BackTranslator | None = None,
        consensus: ConsensusChecker | None = None,
        threshold: float = 0.6,
    ) -> None:
        self.schema = schema
        self.backtranslator = backtranslator
        self.consensus = consensus
        self.threshold = threshold

    def verify(
        self, question: str, generation: SQLGeneration, executed_sql: str, result: QueryResult
    ) -> VerificationReport:
        sql = generation.sql or executed_sql  # pre-guard SQL: without the forced LIMIT
        findings = run_sanity_checks(question, sql, result, generation, self.schema)
        usages: list[CallUsage] = []

        backtranslation = None
        if self.backtranslator is not None:
            try:
                backtranslation = self.backtranslator.check(question, sql, referenced_tables(sql))
                usages += backtranslation.usages
            except LLMError as exc:  # a failed check must not fail the answer
                logger.warning("back-translation unavailable: %s", exc)

        consensus = None
        if self.consensus is not None:
            consensus = self.consensus.check(question, result)
            usages += consensus.usages

        confidence = compute_confidence(
            findings=findings,
            llm_confidence=generation.confidence,
            alignment=backtranslation.judgement.score if backtranslation else None,
            agreement=consensus.agreement if consensus else None,
            threshold=self.threshold,
        )
        logger.info(
            "verification score=%.2f level=%s hallucination=%s",
            confidence.score,
            confidence.level,
            confidence.likely_hallucination,
        )
        return VerificationReport(
            findings=findings,
            backtranslation=backtranslation,
            consensus=consensus,
            confidence=confidence,
            usages=usages,
        )
