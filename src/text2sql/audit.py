"""Append-only audit log (JSON Lines): one record per question, whatever the outcome."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    """Writes one JSON object per line.

    Kept alongside the database history (app.query_history, used by the API): this file is
    append-only, works without the API (CLI) and survives a database reset.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        entry = {"timestamp": datetime.now(UTC).isoformat(), **record}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
