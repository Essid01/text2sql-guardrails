"""Composite confidence score from independent signals.

Each signal is in [0, 1] or None (check disabled or unavailable); the score is the
weighted mean of the available signals. Weights are a documented starting point,
to be calibrated on the evaluation set (Phase 5), not tuned by hand on anecdotes.

The model's self-reported confidence gets a low weight: LLMs are poorly calibrated
about their own errors, which is the whole point of external checks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from text2sql.verification.consensus import Agreement
from text2sql.verification.sanity import Finding

DEFAULT_WEIGHTS: dict[str, float] = {
    "alignment": 0.35,  # back-translation judge
    "agreement": 0.25,  # second independent query returns the same rows
    "sanity": 0.20,  # deterministic result checks
    "schema_coverage": 0.10,  # model's own description consistent with SQL and schema
    "llm_self": 0.10,  # model's self-reported confidence
}
SELF_CONSISTENCY_CHECKS = {"declared_tables_mismatch", "unknown_declared_columns"}


class ConfidenceReport(BaseModel):
    score: float
    level: Literal["high", "medium", "low"]
    likely_hallucination: bool
    components: dict[str, float | None]
    reasons: list[str]  # human-readable explanations of what lowered the score


def sanity_signal(findings: list[Finding]) -> float:
    """1.0 if clean; -0.25 per warning; any error gives 0 (impossible result)."""
    result_findings = [f for f in findings if f.check not in SELF_CONSISTENCY_CHECKS]
    if any(f.severity == "error" for f in result_findings):
        return 0.0
    warnings = sum(f.severity == "warning" for f in result_findings)
    return max(0.0, 1.0 - 0.25 * warnings)


def compute_confidence(
    *,
    findings: list[Finding],
    llm_confidence: float,
    alignment: float | None = None,
    agreement: Agreement | None = None,
    weights: dict[str, float] = DEFAULT_WEIGHTS,
    threshold: float = 0.6,
) -> ConfidenceReport:
    """Combine the signals into a score, a level and a hallucination flag."""
    components: dict[str, float | None] = {
        "alignment": alignment,
        "agreement": {Agreement.MATCH: 1.0, Agreement.MISMATCH: 0.0}.get(agreement)
        if agreement is not None
        else None,
        "sanity": sanity_signal(findings),
        "schema_coverage": 0.5
        if any(f.check in SELF_CONSISTENCY_CHECKS for f in findings)
        else 1.0,
        "llm_self": llm_confidence,
    }
    available = {k: v for k, v in components.items() if v is not None}
    total_weight = sum(weights[k] for k in available)
    score = round(sum(weights[k] * v for k, v in available.items()) / total_weight, 3)

    reasons = [f.message for f in findings if f.severity != "info"]
    if alignment is not None and alignment < 0.75:
        reasons.append(f"the query does not seem to answer the question (alignment {alignment})")
    if agreement == Agreement.MISMATCH:
        reasons.append("an independent second query returned different results")

    level: Literal["high", "medium", "low"] = (
        "high" if score >= 0.8 else "medium" if score >= threshold else "low"
    )
    return ConfidenceReport(
        score=score,
        level=level,
        likely_hallucination=score < threshold,
        components=components,
        reasons=reasons,
    )
