"""python -m text2sql.eval [--only s01,j02] [--no-injected] [--out reports]

Runs the benchmark against the real database and LLM (costs ~$0.10 the first time;
cached afterwards), then writes reports/eval_<timestamp>.{json,md} and reports/latest.*.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess  # noqa: S404  # only `git rev-parse`, fixed arguments
import time
from datetime import UTC, datetime
from pathlib import Path

from text2sql.ask import build_pipeline
from text2sql.config import get_settings
from text2sql.eval.dataset import DEFAULT_PATH, load_cases
from text2sql.eval.metrics import summarize
from text2sql.eval.report import render_markdown
from text2sql.eval.runner import run_all, write_counters


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S603, S607
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the text-to-SQL benchmark.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--no-injected", action="store_true", help="skip injected bad queries")
    parser.add_argument("--no-embedder", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("reports"))
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level="WARNING", format="%(message)s")
    cases = load_cases(args.cases)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    pipeline = build_pipeline(settings, use_embedder=not args.no_embedder)

    before = write_counters(settings.database_url)
    start = time.perf_counter()
    raw = run_all(pipeline, cases, injected=not args.no_injected)
    time.sleep(1.5)  # table statistics are flushed asynchronously
    after = write_counters(settings.database_url)
    writes = sum(after.get(t, 0) - n for t, n in before.items())

    config = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _git_commit(),
        "model": settings.llm_model,
        "effort": settings.llm_effort,
        "embedder": not args.no_embedder,
        "schema_top_k": settings.schema_top_k,
        "backtranslation": settings.verify_backtranslation,
        "multi_query": settings.verify_multi_query,
        "threshold": settings.hallucination_threshold,
        "cases": len(cases),
        "duration_s": round(time.perf_counter() - start),
    }
    summary = summarize(raw["cases"], raw["injected"], settings.estimate_cost_usd, writes)
    payload = {
        "config": config,
        "summary": summary,
        "cases": [c.model_dump() for c in raw["cases"]],
        "injected": [i.model_dump() for i in raw["injected"]],
    }

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    json_path, md_path = args.out / f"eval_{stamp}.json", args.out / f"eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(
        render_markdown(summary, config, raw["cases"], raw["injected"]), encoding="utf-8"
    )
    shutil.copyfile(json_path, args.out / "latest.json")
    shutil.copyfile(md_path, args.out / "latest.md")

    h = summary["hallucination"]["all"]
    print(f"execution accuracy: {summary['execution_accuracy']}")
    print(f"bad queries flagged: {h['bad_flagged']} | false alarms: {h['good_false_alarm']}")
    print(
        f"unsafe refused/blocked: {summary['unsafe_refused_or_blocked']} | "
        f"database writes: {writes}"
    )
    print(f"cost per question: ${summary['cost']['per_question_usd']:.4f} | report: {md_path}")


if __name__ == "__main__":
    main()
