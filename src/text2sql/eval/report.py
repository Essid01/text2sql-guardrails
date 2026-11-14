"""Markdown report from the summary and the per-case results."""

from __future__ import annotations

from typing import Any

from text2sql.eval.runner import CaseResult, InjectedResult


def _pct(r: dict[str, Any]) -> str:
    if r.get("rate") is None:
        return "n/a"
    return f"{100 * r['rate']:.1f} % ({r['passed']}/{r['total']})"


def render_markdown(
    summary: dict[str, Any],
    config: dict[str, Any],
    cases: list[CaseResult],
    injected: list[InjectedResult],
) -> str:
    h = summary["hallucination"]
    lines = [
        "# Evaluation report",
        "",
        " · ".join(f"{k}: `{v}`" for k, v in config.items()),
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Execution accuracy (answerable questions) | {_pct(summary['execution_accuracy'])} |",
        f"| Bad queries flagged (natural + injected) | {_pct(h['all']['bad_flagged'])} |",
        f"| False alarms on correct queries | {_pct(h['all']['good_false_alarm'])} |",
        f"| Unsafe requests refused or blocked | {_pct(summary['unsafe_refused_or_blocked'])} |",
        f"| Database writes during the run | {summary['database_writes_during_eval']} |",
        f"| Clarification asked when needed | {_pct(summary['clarification'])} |",
        f"| Unanswerable questions not answered | {_pct(summary['abstention'])} |",
        f"| Cost per question (all checks) | ${summary['cost']['per_question_usd']:.4f} |",
        f"| Latency p50 / p95 (executed) | {summary['latency_ms_executed']['p50']} / "
        f"{summary['latency_ms_executed']['p95']} ms |",
        "",
        "## Accuracy by category and language",
        "",
        "| Group | Accuracy |",
        "|---|---|",
    ]
    for key in ("accuracy_by_category", "accuracy_by_lang"):
        lines += [f"| {k} | {_pct(v)} |" for k, v in summary[key].items()]
    lines += [
        "",
        "## Hallucination detection",
        "",
        "| Set | Bad flagged | False alarms | AUC |",
        "|---|---|---|---|",
    ]
    for name in ("natural", "injected", "all"):
        d = h[name]
        lines.append(
            f"| {name} | {_pct(d['bad_flagged'])} | {_pct(d['good_false_alarm'])} | {d['auc']} |"
        )
    lines += [
        "",
        "Signal AUC (1.0 = separates bad from good perfectly, 0.5 = chance): "
        + ", ".join(f"{k} {v}" for k, v in h["signal_auc"].items()),
        "",
        "| Threshold | Bad flagged | False alarms |",
        "|---|---|---|",
    ]
    lines += [
        f"| {s['threshold']} | {s['bad_flagged']} | {s['good_false_alarm']} |"
        for s in h["threshold_sweep"]
    ]

    lines += ["", "## Failures", ""]
    failures = [c for c in cases if not c.passed]
    if not failures:
        lines.append("None.")
    for c in failures:
        lines += [
            f"### {c.id} ({c.category}, {c.lang}): {c.question}",
            "",
            f"- expected `{c.expect}`, got `{c.status}`: {c.detail}",
            f"- confidence: {c.confidence} (flagged: {c.likely_hallucination})",
        ]
        if c.sql_executed:
            lines += ["", "```sql", c.sql_executed, "```"]
        if c.gold_sql:
            lines += ["", "Reference:", "", "```sql", c.gold_sql.strip(), "```"]
        lines.append("")

    lines += [
        "## Injected bad queries",
        "",
        "| Case | Mistake | Score | Flagged |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {i.id} | {i.kind} | {i.confidence:.2f} | {i.likely_hallucination} |" for i in injected
    ]
    lines += [
        "",
        "## All cases",
        "",
        "| Case | Expect | Status | Pass | Confidence |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {c.id} | {c.expect} | {c.status} | {'✓' if c.passed else '✗'} | "
        f"{'' if c.confidence is None else f'{c.confidence:.2f}'} |"
        for c in cases
    ]
    return "\n".join(lines) + "\n"
