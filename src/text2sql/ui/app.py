"""Streamlit UI: ask, inspect and edit the SQL, read the confidence report, give feedback.

    streamlit run src/text2sql/ui/app.py        # the API must be running (API_URL)

The UI holds no business logic: every query goes through the API, hence through the
guardrails and the read-only executor, including SQL edited by hand.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from text2sql.config import get_settings
from text2sql.ui.client import APIError, Text2SQLClient

STATUS_LABELS = {
    "executed": ("Executed", "green"),
    "clarification": ("Needs clarification", "orange"),
    "refused": ("Refused", "gray"),
    "blocked": ("Blocked by guardrails", "red"),
    "error": ("Error", "red"),
}
SIGNAL_LABELS = {
    "alignment": "Back-translation matches the question",
    "agreement": "Independent second query agrees",
    "sanity": "Result sanity checks",
    "schema_coverage": "Model's description matches its SQL",
    "llm_self": "Model's self-reported confidence",
}


@st.cache_resource
def _client() -> Text2SQLClient:
    settings = get_settings()
    return Text2SQLClient(settings.api_url, timeout_s=settings.api_timeout_s)


def _unique(columns: list[str]) -> list[str]:
    """Dataframes need unique column names; SQL results may repeat one."""
    seen: dict[str, int] = {}
    out = []
    for c in columns:
        seen[c] = seen.get(c, 0) + 1
        out.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    return out


def _submit(question: str, sql: str | None = None) -> None:
    try:
        with st.spinner("Generating, checking and running the query..."):
            st.session_state.response = _client().query(question, sql)
        st.session_state.question = question
    except APIError as exc:
        st.error(str(exc))


def _show_verification(verification: dict[str, Any]) -> None:
    c = verification["confidence"]
    cols = st.columns(3)
    cols[0].metric("Confidence", f"{c['score']:.2f}", c["level"])
    if c["likely_hallucination"]:
        cols[1].error("Likely hallucination: check the SQL before using this result.")
    for reason in c["reasons"]:
        st.warning(reason)
    with st.expander("Confidence detail"):
        rows = [{"signal": SIGNAL_LABELS.get(k, k),
                 "value": "not run" if v is None else round(v, 2)}
                for k, v in c["components"].items()]  # fmt: skip
        st.table(pd.DataFrame(rows))
        if bt := verification.get("backtranslation"):
            st.markdown(f"**What the SQL actually asks:** {bt['reconstructed_question']}")
            for diff in bt["judgement"]["differences"]:
                st.markdown(f"- {diff}")
        if cons := verification.get("consensus"):
            st.markdown(f"**Second query:** {cons['agreement']} ({cons['detail']})")
            if cons.get("alternate_sql"):
                st.code(cons["alternate_sql"], language="sql")
        for f in verification["findings"]:
            st.markdown(f"- `{f['check']}` ({f['severity']}): {f['message']}")


def _show_feedback(response: dict[str, Any]) -> None:
    query_id = response["query_id"]
    with st.form(f"feedback-{query_id}"):
        st.markdown("**Was this answer correct?**")
        verdict = st.radio("Verdict", ["correct", "incorrect"], horizontal=True,
                           label_visibility="collapsed")  # fmt: skip
        comment = st.text_input("Comment (optional)")
        corrected = st.text_area("Correct SQL (optional)", height=100)
        if st.form_submit_button("Send feedback"):
            try:
                _client().feedback(query_id, verdict, comment, corrected)
                st.success("Thanks: incorrect answers feed the evaluation set after review.")
            except APIError as exc:
                st.error(str(exc))


def _show_response(response: dict[str, Any]) -> None:
    label, color = STATUS_LABELS.get(response["status"], (response["status"], "gray"))
    source = " (your SQL)" if response.get("sql_source") == "user" else ""
    st.markdown(f"### :{color}[{label}]{source}")
    gen = response.get("generation")
    if gen and gen.get("explanation"):
        st.caption(gen["explanation"])

    if response["status"] == "clarification":
        for i, c in enumerate(response["clarifications"]):
            st.info(c["question"])
            buttons = st.columns(len(c["options"]))
            for j, option in enumerate(c["options"]):
                if buttons[j].button(option["label"], key=f"clarify-{i}-{j}"):
                    # Same rule as text2sql.prompt.ambiguity.apply_clarification.
                    _submit(f"{response['question']} ({option['interpretation']})")
                    st.rerun()
    if response.get("refusal_reason"):
        st.info(response["refusal_reason"])
    for v in response.get("violations", []):
        st.error(f"[{v['rule']}] {v['message']}")
    if response.get("error"):
        st.error(response["error"])

    sql = response.get("sql_executed") or (gen or {}).get("sql")
    if sql:
        edited = st.text_area("SQL (editable: re-run goes through the same guardrails)",
                              value=sql, height=160, key=f"sql-{response['query_id']}")  # fmt: skip
        if st.button("Run edited SQL") and edited.strip():
            _submit(response["question"], edited)
            st.rerun()

    if result := response.get("result"):
        st.dataframe(pd.DataFrame(result["rows"], columns=_unique(result["columns"])),
                     width="stretch", hide_index=True)  # fmt: skip
        more = " (truncated)" if result["truncated"] else ""
        st.caption(f"{result['row_count']} rows{more} · {result['duration_ms']:.0f} ms · "
                   f"planner cost {result['estimated_cost']:,.0f}")  # fmt: skip
    if verification := response.get("verification"):
        _show_verification(verification)

    usage = response.get("usage", {})
    calls, cost = usage.get("api_calls", 0), usage.get("cost_usd", 0)
    st.caption(f"{calls:.0f} LLM call(s) · ${cost:.5f} · total {response['duration_ms']:.0f} ms")
    _show_feedback(response)


def _sidebar() -> None:
    st.sidebar.header("History")
    try:
        items = _client().history(limit=20)
    except APIError as exc:
        st.sidebar.error(str(exc))
        return
    for item in items:
        flag = {"correct": " ✓", "incorrect": " ✗"}.get((item.get("feedback") or {}).get(
            "verdict", ""), "")  # fmt: skip
        conf = "" if item["confidence"] is None else f" · {item['confidence']:.2f}"
        label = f"#{item['query_id']} {item['question'][:40]}{flag}"
        if st.sidebar.button(label, key=f"h-{item['query_id']}",
                             help=f"{item['status']}{conf}"):  # fmt: skip
            entry = _client().history_entry(item["query_id"])
            st.session_state.response = {**entry["response"], "query_id": item["query_id"]}
            st.session_state.question = entry["question"]
    with st.sidebar.expander("Database schema"):
        try:
            for table in _client().schema()["tables"]:
                st.markdown(f"**{table['name']}**: "
                            + ", ".join(c["name"] for c in table["columns"]))  # fmt: skip
        except APIError as exc:
            st.error(str(exc))


def main() -> None:
    st.set_page_config(page_title="text2sql-guardrails", layout="wide")
    st.title("Ask the retail database")
    st.caption("Read-only: generated SQL is checked, cost-limited and run by a SELECT-only "
               "role. Each answer comes with a confidence score.")  # fmt: skip
    _sidebar()
    question = st.text_input("Question", value=st.session_state.get("question", ""),
                             placeholder="Top 3 categories by net revenue in 2025")  # fmt: skip
    if st.button("Ask", type="primary") and question.strip():
        _submit(question.strip())
    if response := st.session_state.get("response"):
        _show_response(response)


main()
