"""Local SQLite cache of LLM outputs, to never pay twice for the same call."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from text2sql.llm.client import CallUsage, LLMClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def cache_key(fingerprint: dict[str, Any], system: str, user: str, schema: dict) -> str:
    """Hash of everything that changes the output: model params, prompts, output schema."""
    payload = json.dumps(
        {"llm": fingerprint, "system": system, "user": user, "schema": schema},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CachedLLMClient:
    """Wraps any LLMClient (decorator pattern): same interface, cached results.

    A hit costs nothing (`cost_usd=0`, `api_calls=0`) but keeps the original token
    counts, so evaluation reports can show both real spend and nominal usage.
    """

    def __init__(self, inner: LLMClient, path: str | Path) -> None:
        self.inner = inner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # FastAPI may call from several threads
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS llm_cache ("
                " key TEXT PRIMARY KEY, output TEXT NOT NULL, usage TEXT NOT NULL,"
                " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @property
    def fingerprint(self) -> dict[str, Any]:
        return self.inner.fingerprint

    def complete(self, system: str, user: str, response_model: type[T]) -> tuple[T, CallUsage]:
        key = cache_key(self.fingerprint, system, user, response_model.model_json_schema())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT output, usage FROM llm_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is not None:
            try:
                output = response_model.model_validate_json(row[0])
                usage = CallUsage.model_validate_json(row[1])
            except ValidationError:
                logger.warning("ignoring unreadable cache entry key=%s", key[:12])
            else:
                logger.info("llm cache hit key=%s", key[:12])
                return output, usage.model_copy(
                    update={"cached": True, "cost_usd": 0.0, "api_calls": 0, "duration_ms": 0.0}
                )

        output, usage = self.inner.complete(system, user, response_model)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, output, usage) VALUES (?, ?, ?)",
                (key, output.model_dump_json(), usage.model_dump_json()),
            )
        return output, usage
