"""HTTP API.

    uvicorn text2sql.api.app:app --port 8000      # OpenAPI docs on /docs

Endpoints are plain `def`: the pipeline is synchronous (LLM + DB calls), FastAPI runs
them in its thread pool. Dependencies live in `app.state.service`, so tests inject fakes
through `create_app(service)` without touching the network or the database.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from text2sql import __version__
from text2sql.history import (
    Feedback,
    FeedbackRequest,
    HistoryEntry,
    HistoryItem,
    HistoryStore,
)
from text2sql.pipeline import PipelineResponse, Text2SQLPipeline
from text2sql.schema.models import DatabaseSchema

logger = logging.getLogger(__name__)


@dataclass
class Service:
    pipeline: Text2SQLPipeline
    history: HistoryStore
    schema: DatabaseSchema


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000,
                          examples=["Top 3 categories by net revenue in 2025"])  # fmt: skip
    sql: str | None = Field(
        default=None,
        max_length=10_000,
        description="SQL edited by the user. If set, it is checked by the guardrails and "
        "executed as is (no LLM call, no hallucination checks).",
    )


class QueryResponse(PipelineResponse):
    query_id: int = Field(description="History id, used to send feedback.")


class FeedbackResponse(BaseModel):
    query_id: int
    feedback: Feedback


def _service(request: Request) -> Service:
    service: Service = request.app.state.service
    return service


def create_app(service: Service | None = None) -> FastAPI:
    """Build the app. Without `service`, everything is wired from the settings at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is None:
            app.state.service = build_service()
        else:
            app.state.service = service
        yield

    app = FastAPI(
        title="text2sql-guardrails",
        version=__version__,
        description="Natural-language questions to SQL over a retail PostgreSQL database, "
        "with guardrails (read-only, AST checks, cost limits) and hallucination detection.",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/query", response_model=QueryResponse, tags=["query"])
    def query(body: QueryRequest, request: Request) -> QueryResponse:
        """Answer a question: SQL, result rows and confidence report.

        The HTTP status is 200 whenever the request was processed: the outcome
        (executed, clarification, refused, blocked, error) is in `status`.
        """
        svc = _service(request)
        if body.sql is not None and body.sql.strip():
            response = svc.pipeline.run_user_sql(body.question, body.sql)
        else:
            response = svc.pipeline.ask(body.question)
        query_id = svc.history.record(response)
        return QueryResponse(query_id=query_id, **response.model_dump())

    @app.get("/v1/schema", response_model=DatabaseSchema, tags=["schema"])
    def schema(request: Request) -> DatabaseSchema:
        """Tables, columns, keys and sample values the model is allowed to query."""
        return _service(request).schema

    @app.get("/v1/history", response_model=list[HistoryItem], tags=["history"])
    def history(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[HistoryItem]:
        """Past questions, newest first, with their feedback if any."""
        return _service(request).history.list(limit=limit, offset=offset)

    @app.get("/v1/history/{query_id}", response_model=HistoryEntry, tags=["history"])
    def history_entry(query_id: int, request: Request) -> HistoryEntry:
        entry = _service(request).history.get(query_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown query_id")
        return entry

    @app.put("/v1/history/{query_id}/feedback", response_model=FeedbackResponse,
             tags=["history"])  # fmt: skip
    def feedback(query_id: int, body: FeedbackRequest, request: Request) -> FeedbackResponse:
        """Mark an answer correct or incorrect (latest verdict wins).

        Incorrect answers are exported as candidate evaluation cases:
        `python -m text2sql.feedback`.
        """
        stored = _service(request).history.set_feedback(query_id, body)
        if stored is None:
            raise HTTPException(status_code=404, detail="unknown query_id")
        return FeedbackResponse(query_id=query_id, feedback=stored)

    return app


def build_service() -> Service:
    """Production wiring: pipeline from the settings, history in PostgreSQL."""
    from text2sql.ask import build_pipeline
    from text2sql.config import get_settings
    from text2sql.db.engine import get_engine
    from text2sql.history import PostgresHistoryStore, ensure_app_schema
    from text2sql.schema import introspect

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    ensure_app_schema(settings.database_url)
    schema = introspect(get_engine(settings.database_url))
    pipeline = build_pipeline(settings, schema=schema)
    logger.info("api ready model=%s tables=%d", settings.llm_model, len(schema.tables))
    return Service(pipeline=pipeline, history=PostgresHistoryStore(settings.database_url),
                   schema=schema)  # fmt: skip


app = create_app()
