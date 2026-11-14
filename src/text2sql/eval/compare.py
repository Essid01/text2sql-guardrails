"""Execution accuracy: does a result answer the question like the reference result does?

Rules (documented in the report):
- same number of rows; row order and column order are ignored;
- every value of a reference row must appear in the matched answer row; the answer may
  have extra columns (an id, a label), not fewer;
- text compared case-insensitively; dates as ISO dates (midnight timestamps = dates);
- integers compared exactly; other numbers with a tolerance for rounding (0.051
  absolute or 0.01 % relative): "25.3 %" and "25.34 %" are the same answer.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _norm(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float | Decimal):
        number = float(value)
        if math.isnan(number):
            return None
        return int(number) if isinstance(value, Decimal) and number.is_integer() else number
    if isinstance(value, datetime):
        return value.date() if value.time() == datetime.min.time() else value
    if isinstance(value, date):
        return value
    return str(value).strip().lower()


def _equal(gold: Any, pred: Any) -> bool:
    numbers = (int, float)
    if isinstance(gold, numbers) and isinstance(pred, numbers):
        if isinstance(gold, bool) or isinstance(pred, bool):
            return gold is pred
        if isinstance(gold, int) and isinstance(pred, int):
            return gold == pred
        return abs(gold - pred) <= max(0.051, 1e-4 * abs(gold))
    return bool(gold == pred)


def _row_covers(gold_row: list[Any], pred_row: list[Any]) -> bool:
    """Each gold value matched by a distinct value of the predicted row."""
    available = list(pred_row)
    for g in gold_row:
        for i, p in enumerate(available):
            if _equal(g, p):
                del available[i]
                break
        else:
            return False
    return True


def results_match(gold_rows: list[list[Any]], pred_rows: list[list[Any]]) -> bool:
    if len(gold_rows) != len(pred_rows):
        return False
    gold = [[_norm(v) for v in row] for row in gold_rows]
    pred = [[_norm(v) for v in row] for row in pred_rows]
    # Bipartite matching gold rows -> predicted rows (augmenting paths; small results).
    match: dict[int, int] = {}  # pred index -> gold index

    def assign(g: int, seen: set[int]) -> bool:
        for p in range(len(pred)):
            if p not in seen and _row_covers(gold[g], pred[p]):
                seen.add(p)
                if p not in match or assign(match[p], seen):
                    match[p] = g
                    return True
        return False

    return all(assign(g, set()) for g in range(len(gold)))
