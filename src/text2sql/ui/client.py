"""Thin HTTP client for the API, used by the Streamlit UI (and testable without it)."""

from __future__ import annotations

from typing import Any

import httpx


class APIError(Exception):
    """The API could not be reached or answered with an error."""


class Text2SQLClient:
    def __init__(self, base_url: str, timeout_s: float = 60.0,
                 transport: httpx.BaseTransport | None = None) -> None:  # fmt: skip
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise APIError(f"API unreachable: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text) if response.content else ""
            raise APIError(f"API error {response.status_code}: {detail}")
        return response.json()

    def query(self, question: str, sql: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"question": question}
        if sql:
            payload["sql"] = sql
        result: dict[str, Any] = self._call("POST", "/v1/query", json=payload)
        return result

    def schema(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", "/v1/schema")
        return result

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("GET", "/v1/history", params={"limit": limit})
        return result

    def history_entry(self, query_id: int) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/v1/history/{query_id}")
        return result

    def feedback(self, query_id: int, verdict: str, comment: str | None = None,
                 corrected_sql: str | None = None) -> dict[str, Any]:  # fmt: skip
        body = {"verdict": verdict, "comment": comment or None,
                "corrected_sql": corrected_sql or None}  # fmt: skip
        result: dict[str, Any] = self._call("PUT", f"/v1/history/{query_id}/feedback", json=body)
        return result
