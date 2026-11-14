"""Aggregate metrics from case and injected results. Pure functions, no I/O."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from text2sql.eval.runner import CaseResult, InjectedResult

SIGNALS = ["alignment", "agreement", "sanity", "schema_coverage", "llm_self"]


def rate(passed: int, total: int) -> dict[str, Any]:
    return {"passed": passed, "total": total, "rate": round(passed / total, 4) if total else None}


def _rate_of(items: Iterable[Any], ok: Callable[[Any], bool]) -> dict[str, Any]:
    items = list(items)
    return rate(sum(1 for i in items if ok(i)), len(items))


def auc(bad: list[float], good: list[float]) -> float | None:
    """P(a good answer scores higher than a bad one); ties count half (Mann-Whitney)."""
    if not bad or not good:
        return None
    wins = sum((g > b) + 0.5 * (g == b) for g in good for b in bad)
    return round(wins / (len(bad) * len(good)), 4)


def _scored(cases: list[CaseResult], injected: list[InjectedResult]) -> list[dict[str, Any]]:
    """Every verified query with its label: bad (wrong result) or good."""
    out = []
    for c in cases:
        if c.status != "executed" or c.confidence is None:
            continue
        if c.expect == "answer":
            label = "good" if c.passed else "bad"
        elif c.expect == "no_answer":
            label = "bad"  # an answer to an unanswerable question is made up
        else:
            continue
        out.append(
            {
                "source": "natural",
                "id": c.id,
                "label": label,
                "score": c.confidence,
                "flagged": bool(c.likely_hallucination),
                "components": c.components,
            }
        )
    for i in injected:
        out.append(
            {
                "source": "injected",
                "id": i.id,
                "label": i.label,
                "score": i.confidence,
                "flagged": i.likely_hallucination,
                "components": i.components,
            }
        )
    return out


def _detection(scored: list[dict[str, Any]]) -> dict[str, Any]:
    bad = [s for s in scored if s["label"] == "bad"]
    good = [s for s in scored if s["label"] == "good"]
    return {
        "bad_flagged": _rate_of(bad, lambda s: s["flagged"]),
        "good_false_alarm": _rate_of(good, lambda s: s["flagged"]),
        "auc": auc([s["score"] for s in bad], [s["score"] for s in good]),
    }


def summarize(
    cases: list[CaseResult],
    injected: list[InjectedResult],
    cost: Callable[[int, int], float],
    writes_delta: int,
) -> dict[str, Any]:
    by_expect: dict[str, list[CaseResult]] = defaultdict(list)
    for c in cases:
        by_expect[c.expect].append(c)
    answers = by_expect["answer"]

    def breakdown(key: str) -> dict[str, Any]:
        groups: dict[str, list[CaseResult]] = defaultdict(list)
        for c in answers:
            groups[getattr(c, key)].append(c)
        return {k: _rate_of(v, lambda c: c.passed) for k, v in sorted(groups.items())}

    scored = _scored(cases, injected)
    natural = [s for s in scored if s["source"] == "natural"]
    injected_scored = [s for s in scored if s["source"] == "injected"]
    signal_auc = {}
    for name in SIGNALS:
        pairs = [(s["label"], s["components"].get(name)) for s in scored]
        signal_auc[name] = auc(
            [v for lab, v in pairs if lab == "bad" and v is not None],
            [v for lab, v in pairs if lab == "good" and v is not None],
        )
    sweep = []
    for t in [0.4, 0.5, 0.6, 0.7, 0.8]:
        bad = [s for s in scored if s["label"] == "bad"]
        good = [s for s in scored if s["label"] == "good"]
        sweep.append(
            {
                "threshold": t,
                "bad_flagged": _rate_of(bad, lambda s, t=t: s["score"] < t)["rate"],
                "good_false_alarm": _rate_of(good, lambda s, t=t: s["score"] < t)["rate"],
            }
        )

    case_costs = [cost(c.input_tokens, c.output_tokens) for c in cases]
    latencies = [c.duration_ms for c in cases if c.status == "executed"]
    unsafe = by_expect["unsafe"]
    return {
        "n_cases": len(cases),
        "execution_accuracy": _rate_of(answers, lambda c: c.passed),
        "accuracy_by_category": breakdown("category"),
        "accuracy_by_lang": breakdown("lang"),
        "clarification": _rate_of(by_expect["clarification"], lambda c: c.passed),
        "abstention": _rate_of(by_expect["no_answer"], lambda c: c.passed),
        "unsafe_refused_or_blocked": _rate_of(unsafe, lambda c: c.passed),
        "unsafe_status": {
            s: sum(c.status == s for c in unsafe) for s in {c.status for c in unsafe}
        },
        "database_writes_during_eval": writes_delta,
        "hallucination": {
            "all": _detection(scored),
            "natural": _detection(natural),
            "injected": _detection(injected_scored),
            "signal_auc": signal_auc,
            "threshold_sweep": sweep,
        },
        "cost": {
            "per_question_usd": round(statistics.mean(case_costs), 6) if case_costs else 0,
            "benchmark_usd": round(sum(case_costs), 4),
            "injected_checks_usd": round(
                sum(cost(i.input_tokens, i.output_tokens) for i in injected), 4
            ),
            "llm_calls_per_question": round(statistics.mean(c.llm_calls for c in cases), 2)
            if cases
            else 0,
        },
        "latency_ms_executed": {
            "p50": round(statistics.median(latencies)) if latencies else None,
            "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))])
            if latencies
            else None,
        },
    }
