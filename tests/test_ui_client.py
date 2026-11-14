"""HTTP client used by the Streamlit UI (mocked transport, no server)."""

import json

import httpx
import pytest

from text2sql.ui.client import APIError, Text2SQLClient


def _client(handler) -> Text2SQLClient:
    return Text2SQLClient("http://api", transport=httpx.MockTransport(handler))


def test_ui_client_sends_edited_sql_only_when_given() -> None:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.read())
        return httpx.Response(200, json={"status": "executed"})

    client = _client(handler)
    client.query("q")
    client.query("q", "SELECT 1")
    assert json.loads(sent[0]) == {"question": "q"}
    assert json.loads(sent[1]) == {"question": "q", "sql": "SELECT 1"}


def test_ui_client_reports_api_errors() -> None:
    client = _client(lambda r: httpx.Response(404, json={"detail": "unknown query_id"}))
    with pytest.raises(APIError, match="404: unknown query_id"):
        client.history_entry(1)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(APIError, match="unreachable"):
        _client(down).history()
