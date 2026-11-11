"""One group's decisions and their outcomes, as `trimset_stats.dataset` items (T2.3).

A group is (question, served model); the type comes with the question (FR-15). Refusals
have no probabilities and shadows answer someone else's traffic, so neither is read.
Probabilities are renormalised here: vendors round them, so a row may sum to 1 only
within 1e-3 (docs/03-api/wire-formats.md §3).
"""

import json
import uuid
from datetime import datetime
from typing import Any

from trimset_server.questions import definitions
from trimset_server.records import QuestionIn, fits
from trimset_stats.dataset import Item


def label_index(question: QuestionIn, label: Any) -> int | None:
    """The class a label names, in question order (noul: no 0, yes 1), or None when it
    doesn't fit the question."""
    if not fits(question, label):
        return None
    if question.type == "noul":
        return int(label)
    if question.type == "choice":
        options = question.options or []
        return next(
            i for i, o in enumerate(options) if type(o.value) is type(label) and o.value == label
        )
    return int(label)


def _classes(question: QuestionIn, probs: list[float]) -> tuple[float, ...] | None:
    values = [float(p) for p in probs]
    if question.type == "noul":
        values = [1.0 - values[0], values[0]]
    total = sum(values)
    if len(values) != max(2, question.size()) or total <= 0:
        return None
    return tuple(v / total for v in values)


async def load_items(
    conn: Any,
    project_id: uuid.UUID,
    question_id: str,
    model_served: str,
    *,
    since: datetime,
) -> tuple[QuestionIn, list[Item]]:
    """The question, and its decisions from `since` on with their current outcome."""
    question = (await definitions(conn, project_id, {question_id})).get(question_id)
    if question is None:
        raise LookupError(f"no question {question_id} in this project")
    rows = await conn.fetch(
        "SELECT d.decision_id, d.ts, d.probs, d.action_taken, d.audit, d.external_ref, "
        "o.label, o.observed_at "
        "FROM decisions d LEFT JOIN outcomes o "
        "ON o.project_id = d.project_id AND o.decision_id = d.decision_id "
        "WHERE d.project_id = $1 AND d.question_id = $2 AND d.model_served = $3 "
        "AND d.ts >= $4 AND d.role = 'primary' AND NOT d.refusal "
        "ORDER BY d.ts, d.decision_id",
        project_id,
        question_id,
        model_served,
        since,
    )
    items = []
    for r in rows:
        probs = _classes(question, r["probs"])
        if probs is None:
            continue
        label = None if r["label"] is None else label_index(question, json.loads(r["label"]))
        items.append(
            Item(
                decision_id=str(r["decision_id"]),
                ts=r["ts"].timestamp(),
                probs=probs,
                label=label,
                labelled_at=r["observed_at"].timestamp() if label is not None else None,
                action=r["action_taken"],
                audit=r["audit"],
                external_ref=r["external_ref"],
            )
        )
    return question, items
