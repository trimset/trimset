"""The client (sdk.md): a policy cache kept fresh by a background thread, gate(), which
applies it locally, and the calls that report back to the server (log_exchange, log,
outcome, instrument), which only queue for the sending thread (sender.py).

gate() never raises and never touches the network (ADR 0012): without a usable policy it
returns the question's default, with a reason. The reporting calls never raise or block
either: a failure is logged, never handed to the caller.
"""

import atexit
import base64
import contextlib
import functools
import json
import logging
import math
import os
import random
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from trimset import policy as policies
from trimset.hashing import fingerprint, question_id, state_hash
from trimset.sender import Sender
from trimset.wire import (
    SUM_TOLERANCE,
    Format,
    WireError,
    _probabilities,
    parse_exchange,
    to_records,
    uuid7,
)

log = logging.getLogger("trimset")
ACTIONS = ("ACCEPT", "BLOCK", "ESCALATE")
LIBRARY_DEFAULT = "ESCALATE"
MAX_BACKOFF_S = 60.0
SOURCES = ("human_review", "downstream", "rule")
F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class GateResult:
    action: str  # ACCEPT, BLOCK, ESCALATE or SELECT:<value>
    label: Any = None  # the selected option's value
    source: str = "default"  # policy, audit or default
    reason: str | None = None  # why the default was used
    policy_version: int | None = None
    policy_fingerprint: str | None = None
    decision_id: str = ""
    audit: bool = False
    scalar: float | None = None


@dataclass(frozen=True)
class Snapshot:
    """One verified bundle. gate() reads it without locks; the poller swaps a new one in."""

    questions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # name or id
    policies: Mapping[tuple[str, str], Mapping[str, Any]] = field(default_factory=dict)
    with_policy: frozenset[str] = frozenset()  # question ids with a policy for some model
    broken: frozenset[tuple[str, str]] = frozenset()  # failed fingerprints, no good version
    etag: str | None = None
    fetched_at: float | None = None  # wall clock of the last good answer, 200 or 304
    state_key: bytes | None = None  # the project's key for state hashes (ADR 0009)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect would carry the key elsewhere


_open = urllib.request.build_opener(_NoRedirect).open


def build_snapshot(
    bundle: Mapping[str, Any], previous: Snapshot, fetched_at: float, etag: str | None = None
) -> Snapshot:
    """Verifies every policy's fingerprint (FR-34). A policy that fails is dropped, and
    the previous good version of its group, if any, is kept instead."""
    if bundle.get("schema") != "bundle/1":
        raise ValueError("not a bundle/1")
    questions: dict[str, Mapping[str, Any]] = {}
    for name, q in bundle["questions"].items():
        questions[name] = questions[q["question_id"]] = q
    kept: dict[tuple[str, str], Mapping[str, Any]] = {}
    broken = set()
    for entry in bundle["policies"]:
        p = entry["policy"]
        group = (p["question_id"], p["model_served"])
        if fingerprint(p) == p.get("fingerprint"):
            kept[group] = entry
        elif group in previous.policies:
            log.warning("policy %s failed its fingerprint; keeping the last good one", group)
            kept[group] = previous.policies[group]
        else:
            log.warning("policy %s failed its fingerprint and is not used", group)
            broken.add(group)
    key = bundle.get("state_hash_key") or {}
    return Snapshot(
        state_key=base64.b64decode(key["key_b64"]) if key.get("key_b64") else None,
        questions=questions,
        policies=kept,
        with_policy=frozenset(g[0] for g in kept) | frozenset(g[0] for g in broken),
        broken=frozenset(broken),
        etag=etag or bundle.get("etag"),
        fetched_at=fetched_at,
    )


class Client:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        defaults: Mapping[str, str] | None = None,
        default_model: str | None = None,
        poll_interval: float = 30.0,
        poll_jitter: float = 0.2,
        max_policy_age: float | None = None,
        cache_path: str | os.PathLike[str] | None = None,
        timeout: float = 5.0,
        buffer_size: int = 10_000,
        batch_size: int = 100,
        flush_interval: float = 1.0,
    ) -> None:
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise ValueError("base_url must start with http:// or https://")
        if not key:
            raise ValueError("key is required: an ingest key, tsi_…")
        for question, action in (defaults or {}).items():
            if action not in ACTIONS:
                raise ValueError(f"the default for {question} must be one of {ACTIONS}")
        self.base_url, self._key = base_url.rstrip("/"), key
        self.defaults = dict(defaults or {})
        self.default_model = default_model
        self.poll_interval, self.poll_jitter = poll_interval, poll_jitter
        self.max_policy_age, self.timeout = max_policy_age, timeout
        self.cache_path = Path(cache_path) if cache_path else None
        self._snapshot = Snapshot()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._warned: set[str] = set()
        self._pid: int | None = None
        self._thread: threading.Thread | None = None
        self._sender = Sender(
            self.base_url, key, timeout=timeout, buffer_size=buffer_size,
            batch_size=batch_size, flush_interval=flush_interval,
        )  # fmt: skip
        self._load_cache()
        self._ensure_thread()
        ref = weakref.ref(self)  # at exit, flush for at most 2 s (sdk.md §5)
        atexit.register(lambda: (c := ref()) is not None and c.flush(2.0))

    # ------------------------------------------------------------ the policy cache

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def policy_age_seconds(self) -> float | None:
        """trimset_policy_age_seconds: since the server last confirmed the policies."""
        at = self._snapshot.fetched_at
        return None if at is None else max(0.0, time.time() - at)

    def wait_for_policies(self, timeout: float = 2.0) -> bool:
        """Waits until a bundle has loaded; the only call that may wait on the network."""
        self._ensure_thread()
        return self._ready.wait(timeout)

    def flush(self, timeout: float = 2.0) -> bool:
        """Sends what is queued; True when it all went within `timeout`."""
        return self._sender.flush(timeout)

    @property
    def dropped(self) -> int:
        """Records, reports and outcomes dropped: a full queue, or refused by the server."""
        return self._sender.dropped_total

    def close(self, timeout: float = 2.0) -> None:
        """Flushes for at most `timeout`, then stops both threads."""
        self._stop.set()
        self._sender.close(timeout)
        if self._thread is not None and self._pid == os.getpid():
            self._thread.join(timeout=0.5)  # a daemon: one stuck on a dead server can't hold us

    def _ensure_thread(self) -> None:
        """Starts the poller, and starts it again in a forked child (gunicorn preload)."""
        self._sender.ensure_thread()
        if self._pid == os.getpid() or self._stop.is_set():
            return
        self._pid = os.getpid()
        self._thread = threading.Thread(target=self._poll_loop, name="trimset-poll", daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self.refresh()
                failures = 0
                j = self.poll_jitter
                delay = self.poll_interval * random.uniform(1 - j, 1 + j)  # noqa: S311 - jitter
            except Exception as e:  # keep the last good bundle and back off
                failures += 1
                delay = random.uniform(0, min(MAX_BACKOFF_S, 0.5 * 2**failures))  # noqa: S311
                log.warning("trimset: can't fetch policies (%s); retrying", type(e).__name__)
            self._stop.wait(delay)

    def refresh(self) -> None:
        """One poll: GET /v1/policies with If-None-Match. Raises on failure."""
        headers = {"Authorization": f"Bearer {self._key}", "User-Agent": "trimset-python"}
        if self._snapshot.etag:
            headers["If-None-Match"] = f'"{self._snapshot.etag}"'
        url = self.base_url + "/v1/policies"  # http(s) only, checked in __init__
        request = urllib.request.Request(url, headers=headers)  # noqa: S310
        try:
            with _open(request, timeout=self.timeout) as response:
                raw = response.read()
                etag = (response.headers.get("ETag") or "").strip('"') or None
        except urllib.error.HTTPError as e:
            if e.code != 304:
                raise
            self._snapshot = replace(self._snapshot, fetched_at=time.time())
            self._ready.set()
            return
        bundle = json.loads(raw)
        self._snapshot = build_snapshot(bundle, self._snapshot, time.time(), etag)
        self._ready.set()
        self._save_cache(bundle)

    def _save_cache(self, bundle: Mapping[str, Any]) -> None:
        if self.cache_path is None:
            return
        try:  # atomically: a reader sees the old file or the new one
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.cache_path.parent, prefix=".trimset-")
            with os.fdopen(fd, "w") as f:
                json.dump(bundle, f)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning("trimset: can't write the policy cache (%s)", type(e).__name__)
            with contextlib.suppress(OSError, UnboundLocalError):
                os.unlink(tmp)

    def _load_cache(self) -> None:
        if self.cache_path is None:
            return
        try:
            bundle = json.loads(self.cache_path.read_text())
            at = self.cache_path.stat().st_mtime
            self._snapshot = build_snapshot(bundle, Snapshot(), at)
            self._ready.set()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("trimset: ignoring the policy cache (%s)", type(e).__name__)

    # ------------------------------------------------------------ gate

    def gate(
        self,
        question: str,
        *,
        p: float | None = None,
        probabilities: Sequence[float] | None = None,
        answer: Any = None,
        model_served: str | None = None,
        decision_id: str | None = None,
        question_def: Mapping[str, Any] | None = None,
        default: str | None = None,
    ) -> GateResult:
        """The action for one answer, from the live policy (sdk.md §3). Never raises."""
        did = decision_id if isinstance(decision_id, str) and decision_id else uuid7()
        try:
            self._ensure_thread()
            result = self._gate(
                question, p, probabilities, answer, model_served, did, question_def, default
            )
        except Exception:
            log.exception("trimset: gate() failed; returning the default")
            result = self._default(question, default, "no_policy", did)
        if did is decision_id:  # logged elsewhere (the proxy): tell the server what we did
            with contextlib.suppress(Exception):
                self._sender.add("actions", [_action_report(result)])
        return result

    # ------------------------------------------------------------ reporting back

    def log_exchange(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        *,
        format: Format,
        gates: Mapping[str, GateResult] | None = None,
        external_ref: str | None = None,
        tags: Mapping[str, str] | None = None,
        latency_ms: float | None = None,
    ) -> list[str]:
        """Records one call to a decision model: a record per answered question, with the
        action from `gates` (by question name). The state is hashed here, with the project's
        key, then dropped; it is never sent. Returns the decision ids in question order."""
        try:
            exchange = parse_exchange(request, response, format)
            gated = [(gates or {}).get(d.name or "") for d in exchange.decisions]
            ids = [g.decision_id if g else uuid7() for g in gated]
            key = self._snapshot.state_key
            hashed = state_hash(key, request["state"]) if key and "state" in request else None
            records = to_records(
                exchange, ts=datetime.now(UTC), request_id=uuid7(), decision_ids=ids,
                state_hash=hashed, latency_ms=latency_ms, tags=tags, external_ref=external_ref,
            )  # fmt: skip
            for record, g in zip(records, gated, strict=True):
                if g:
                    record |= _action_fields(g)
            questions = {d.question_id: d.definition for d in exchange.decisions}
            self._sender.add("records", records, questions)
            return ids
        except Exception as e:
            log.warning("trimset: couldn't log an exchange (%s)", type(e).__name__)
            return []

    def log(self, record: Mapping[str, Any]) -> None:
        """Queues one record/1 as it is, for clients that build their own. No question
        definition goes with it: the server must already know the question."""
        with contextlib.suppress(Exception):
            self._sender.add("records", [dict(record)])

    def outcome(
        self,
        *,
        decision_id: str | None = None,
        external_ref: str | None = None,
        question: str | None = None,
        label: Any,
        source: str,
        observed_at: datetime | str | None = None,
        outcome_id: str | None = None,
    ) -> None:
        """Queues what really happened (sdk.md §6). Only the shape is checked here; the
        server checks the label against the question. Never raises."""
        if (decision_id is None) == (external_ref is None) or (external_ref and not question):
            log.warning("trimset: outcome() needs a decision_id, or an external_ref and question")
            return
        if source not in SOURCES:
            log.warning("trimset: outcome() source must be one of %s", ", ".join(SOURCES))
            return
        when = observed_at or datetime.now(UTC)
        item: dict[str, Any] = {
            "schema": "outcome/1",
            "label": label,
            "source": source,
            "observed_at": when.isoformat() if isinstance(when, datetime) else when,
        }
        if decision_id is not None:
            item["decision_id"] = decision_id
        else:
            item |= {"external_ref": external_ref, "question": question}
        if outcome_id is not None:
            item["outcome_id"] = outcome_id
        with contextlib.suppress(Exception):
            self._sender.add("outcomes", [item])

    def instrument(self, model_callable: F, *, model: str, format: Format = "systemone") -> F:
        """The same callable, logging every call as log_exchange() would (sdk.md §2). For
        a model run in this process, e.g. a Hugging Face pipeline: it takes the request
        without `model` and may answer bare. `model` is the served model, `<repo>@<revision>`.
        The model's answers are returned untouched; a logging failure never reaches here."""

        @functools.wraps(model_callable)
        def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            response = model_callable(request, *args, **kwargs)
            try:
                latency = (time.perf_counter() - start) * 1000
                sent = dict(request)
                sent.setdefault("model", model)
                got = dict(response) if "answers" in response else {"answers": response}
                got.setdefault("model", model)
                self.log_exchange(sent, got, format=format, latency_ms=latency)
            except Exception as e:
                log.warning("trimset: couldn't log a call to %s (%s)", model, type(e).__name__)
            return response

        return wrapper  # type: ignore[return-value]

    def _default(self, question: Any, given: Any, reason: str, did: str) -> GateResult:
        """The action when no policy applies. Total: whatever the caller passed, it returns
        one of ACCEPT, BLOCK or ESCALATE."""
        name = question if isinstance(question, str) else ""
        if given is not None and given not in ACTIONS and "bad-default" not in self._warned:
            self._warned.add("bad-default")
            log.warning("trimset: ignoring default=%r; it must be one of %s", given, ACTIONS)
        action = given if given in ACTIONS else self.defaults.get(name)
        if action is None:
            q = self._snapshot.questions.get(name)
            names = [
                n for n, v in self._snapshot.questions.items() if v is q and n in self.defaults
            ]
            action = self.defaults[names[0]] if names else None
        if action is None:
            action = LIBRARY_DEFAULT
            if name not in self._warned:
                self._warned.add(name)
                log.warning("trimset: no default for %s; using %s", name, LIBRARY_DEFAULT)
        return GateResult(action=action, source="default", reason=reason, decision_id=did)

    def _gate(
        self,
        question: str,
        p: float | None,
        probabilities: Sequence[float] | None,
        answer: Any,
        model_served: str | None,
        did: str,
        question_def: Mapping[str, Any] | None,
        default: str | None,
    ) -> GateResult:
        snap = self._snapshot

        def fallback(reason: str) -> GateResult:
            return self._default(question, default, reason, did)

        # 1. The question.
        q = snap.questions.get(question)
        if q is None:
            return fallback("no_policy")
        qid, definition = q["question_id"], q["definition"]
        if question_def is not None:
            try:
                if question_id(question_def) != qid:
                    return fallback("question_changed")
            except (TypeError, ValueError, KeyError):
                return fallback("question_changed")

        # 2. The probabilities.
        if isinstance(answer, Mapping) and answer.get("type") == "refusal":
            return fallback("refusal")
        probs = _read_probabilities(definition, p, probabilities, answer)
        if probs is None:
            return fallback("invalid_probabilities")

        # 3. The policy.
        model = model_served or self.default_model
        if model is None:
            return fallback("model_unknown")
        entry = snap.policies.get((qid, model))
        if entry is None:
            if (qid, model) in snap.broken:
                return fallback("fingerprint_mismatch")
            return fallback("model_mismatch" if qid in snap.with_policy else "no_policy")
        age = self.policy_age_seconds
        if self.max_policy_age is not None and (age is None or age > self.max_policy_age):
            return fallback("stale_policy")
        policy = entry["policy"]
        version, fp = policy["version"], policy["fingerprint"]

        # 4. Audit sampling (FR-37).
        if policies.audited(did, float(policy.get("audit_fraction") or 0.0)):
            return GateResult(
                action="ESCALATE", source="audit", policy_version=version,
                policy_fingerprint=fp, decision_id=did, audit=True,
            )  # fmt: skip

        # 5 to 8. Calibrate, read the scalars, check the rules.
        applied = policies.apply(policy, definition, probs)
        return GateResult(
            action=applied.action, label=applied.label, source="policy", policy_version=version,
            policy_fingerprint=fp, decision_id=did, scalar=applied.scalar,
        )  # fmt: skip


def _read_probabilities(
    definition: Mapping[str, Any],
    p: float | None,
    probabilities: Sequence[float] | None,
    answer: Any,
) -> list[float] | None:
    """Step 2: [p] for noul, else one per option or level summing to exactly 1; None when
    the input is invalid."""
    if sum(x is not None for x in (p, probabilities, answer)) != 1:
        return None
    kind = definition["type"]
    if answer is not None:
        formats: tuple[Format, ...] = ("systemone", "openai-decisions")
        for fmt in formats:
            try:
                found = _probabilities(definition, answer, fmt)
                break
            except (WireError, KeyError, TypeError, AttributeError, ValueError):
                continue
        else:
            return None
    elif p is not None:
        if kind != "noul":
            return None
        found = [p]
    else:
        if kind == "noul" or probabilities is None:
            return None
        size = len(definition["options"] if kind == "choice" else definition["levels"])
        found = list(probabilities)
        if len(found) != size:
            return None
    if not all(
        isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1
        for x in found
    ):
        return None
    if kind == "noul":
        return [float(found[0])]
    total = sum(found)
    if abs(total - 1) > SUM_TOLERANCE:
        return None
    return [float(x) / total for x in found]


def _action_fields(d: GateResult) -> dict[str, Any]:
    return {
        "action_taken": d.action,
        "action_source": d.source,
        "policy_version": d.policy_version,
        "policy_fingerprint": d.policy_fingerprint,
        "audit": d.audit,
    }


def _action_report(d: GateResult) -> dict[str, Any]:
    """An ActionReport (POST /v1/decisions:actions) for a decision logged elsewhere."""
    return {"decision_id": d.decision_id, "reason": d.reason} | _action_fields(d)
