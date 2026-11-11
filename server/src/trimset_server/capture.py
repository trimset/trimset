"""Exchanges the proxy forwarded, turned into decision records (FR-04 to FR-08).

The proxy itself is Rust (proxy/, ADR 0015): it forwards, picks one decision id per
question, and hands the exchange here (internal.py). Reading the wire formats, hashing and
storing stay in Python, next to the SDK they share (docs/03-api/wire-formats.md).
"""

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import unquote

from pydantic import ValidationError

from trimset.hashing import project_state_key, state_hash
from trimset.wire import WireError, parse_exchange, request_cost, to_records, uuid7
from trimset_server.ingest import has_nul
from trimset_server.projects import ProjectSettings
from trimset_server.recorder import Pending
from trimset_server.records import RecordIn
from trimset_server.upstreams import Format

log = logging.getLogger("trimset.capture")


def metadata(ref_header: str | None, tags_header: str | None) -> tuple[str | None, dict[str, str]]:
    """X-Trimset-External-Ref and X-Trimset-Tags ("k=v,k2=v2", URL-encoded)."""
    ref = ref_header or None
    tags: dict[str, str] = {}
    for pair in (tags_header or "").split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            tags[unquote(key.strip())] = unquote(value.strip())
    return ref, tags


def _state_key(state: Any, project_id: uuid.UUID) -> bytes | None:
    secret = state.settings.secret
    if secret is None:
        return None
    cache: dict[uuid.UUID, bytes] = state.state_keys
    if project_id not in cache:
        cache[project_id] = project_state_key(secret, str(project_id))
    return cache[project_id]


def capture(
    state: Any,
    project_id: uuid.UUID,
    settings: ProjectSettings,
    fmt: Format,
    sent: Any,
    content: bytes,
    latency_ms: float,
    *,
    ids: Sequence[str],
    ts: datetime,
    ref: str | None,
    tags: dict[str, str],
) -> int | None:
    """Parse one exchange and queue its records, under the ids the proxy gave each
    question by position. Returns how many were queued, or None when the exchange can't
    be read; it was forwarded anyway and is never logged. A question that can't be read
    keeps its id, which never gets a record."""
    try:
        exchange = parse_exchange(sent, json.loads(content), fmt)
    except (ValueError, WireError) as e:
        log.info("exchange not logged (%s)", type(e).__name__)
        return None
    if exchange.skipped:
        log.info("%d questions not logged", len(exchange.skipped))
    if any(d.position >= len(ids) for d in exchange.decisions):
        log.warning("exchange not logged: %d ids for its questions", len(ids))
        return None

    key = _state_key(state, project_id)
    try:
        hashed = state_hash(key, exchange.state) if key else None
    except ValueError:
        hashed = None  # a state JSON can't carry exactly (huge integers): no hash
    if dropped := settings.disallowed_tags(tags):
        tags = {k: v for k, v in tags.items() if k not in dropped}
        log.info("dropped %d tags not on the project's allow-list", len(dropped))
    records = to_records(
        exchange,
        ts=ts,
        request_id=uuid7(),
        decision_ids=[ids[d.position] for d in exchange.decisions],
        state_hash=hashed,
        latency_ms=latency_ms,
        cost_usd=request_cost(exchange.usage, settings.price_for(exchange.model_served)),
        tags=tags,
        external_ref=ref,
    )

    queued: list[Pending] = []
    for decision, raw in zip(exchange.decisions, records, strict=True):
        if has_nul(raw) or has_nul(decision.definition):
            continue
        try:
            record = RecordIn.model_validate(raw)
        except ValidationError:
            # Bad metadata from headers never costs the decision itself.
            try:
                record = RecordIn.model_validate(raw | {"tags": {}, "external_ref": None})
                log.info("ignored invalid X-Trimset-Tags or X-Trimset-External-Ref")
            except ValidationError:
                continue
        queued.append(Pending(project_id, record, decision.definition, decision.name))
    state.recorder.add(queued)
    return len(queued)
