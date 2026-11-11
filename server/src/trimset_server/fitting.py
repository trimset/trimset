"""Fits, compiles and the simulator (T2.6, FR-14 to FR-26).

- A **fit** (a job) loads one group, splits it (trimset_stats.dataset), chooses a
  calibration map on the fit split, and stores fit/1 with raw-against-calibrated metrics
  on the eval split. The tune and eval arrays are kept beside it, without ids, so a
  compile or a simulation never rereads the decisions.
- A **compile** (a job) turns a fit and a target into a draft policy/1 with its
  fingerprint, or reports not_enough_labels or infeasible.
- The **simulator** (synchronous) scores candidate rules on a fit's eval split.

The statistics run in a thread, never on an event loop (ADR 0001).
"""

import asyncio
import io
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from trimset.hashing import fingerprint
from trimset_server.datasets import load_items
from trimset_server.questions import definitions
from trimset_server.records import QuestionIn
from trimset_stats import calibration, compiler, metrics
from trimset_stats.calibration import Calibrator
from trimset_stats.compiler import Actions, Compiled, Costs, Rule, Target
from trimset_stats.dataset import Dataset, Split, build

FOLDS = 5
BINS = metrics.DEFAULT_BINS
SPLIT_SHARES = {"fit": 0.4, "tune": 0.4, "eval": 0.2}


class JobError(Exception):
    """A job that can't succeed by trying again: the message is shown as the job's error."""


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def seed_of(fit_id: uuid.UUID) -> int:
    return int.from_bytes(fit_id.bytes[:4], "big") & 0x7FFFFFFF


async def _settings(
    conn: Any, project_id: uuid.UUID, question_id: str
) -> tuple[dict[str, Any], str | None]:
    row = await conn.fetchrow(
        "SELECT settings, name FROM questions WHERE project_id = $1 AND question_id = $2",
        project_id,
        question_id,
    )
    if row is None:
        raise JobError("No such question in this project")
    return json.loads(row["settings"]), row["name"]


# ------------------------------------------------------------------ fits


def _events(
    question: QuestionIn, probs: np.ndarray, labels: np.ndarray, cal: Calibrator
) -> tuple[Any, Any, Any]:
    """The raw and calibrated probability of the event a policy reads, and its outcome:
    P(yes) for noul, the top option for choice and score (fit/1)."""
    if question.type == "noul":
        p = probs[:, 1]
        return p, cal.apply(p), (labels == 1).astype(float)
    raw_p, raw_y = metrics.events(probs, labels, "top")
    cal_p, cal_y = metrics.events(cal.apply(probs), labels, "top")
    return (raw_p, raw_y), (cal_p, cal_y), None


def _metrics(p: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    return {
        "n": int(p.size),
        "log_loss": metrics.log_loss(p, y),
        "brier": metrics.brier(p, y),
        "ece_equal_width": metrics.ece(p, y, BINS, "width"),
        "ece_equal_mass": metrics.ece(p, y, BINS, "mass"),
        "bins": BINS,
    }


def _bins(p: np.ndarray, y: np.ndarray) -> list[dict[str, Any]]:
    return [
        {
            "lo": b.lower,
            "hi": b.upper,
            "count": b.count,
            "mean_predicted": b.mean_predicted,
            "observed": b.observed,
            "ci95": [b.low, b.high],
        }
        for b in metrics.reliability(p, y, BINS)
    ]


def _pack(data: Dataset) -> bytes:
    out = io.BytesIO()
    tune, ev = data.tune, data.eval
    np.savez_compressed(
        out,
        tune_probs=tune.probs,
        tune_labels=tune.labels,
        tune_audit=tune.audit,
        eval_probs=ev.probs,
        eval_labels=ev.labels,
        eval_audit=ev.audit,
    )
    return out.getvalue()


def unpack(blob: bytes) -> tuple[Split, Split]:
    with np.load(io.BytesIO(blob)) as z:
        parts = []
        for name in ("tune", "eval"):
            labels = z[f"{name}_labels"]
            parts.append(Split(("",) * labels.size, z[f"{name}_probs"], labels, z[f"{name}_audit"]))
    return parts[0], parts[1]


def compute_fit(
    question: QuestionIn,
    question_id: str,
    model_served: str,
    items: Sequence[Any],
    *,
    fit_id: uuid.UUID,
    now: datetime,
    window_days: int,
    horizon_days: int,
) -> tuple[dict[str, Any], bytes, list[str]] | dict[str, Any]:
    """fit/1, the packed splits and the labeled tuning items' ids, or a not_enough_labels
    result."""
    seed = seed_of(fit_id)
    data = build(
        items, now=now.timestamp(), seed=seed, window_days=window_days, horizon_days=horizon_days
    )
    if data.needed:
        return {"not_enough_labels": {"labeled": data.labeled, "needed": data.needed}}
    fit, ev = data.fit, data.eval
    f_lab, e_lab = fit.labeled, ev.labeled
    try:
        if question.type == "noul":
            choice = calibration.choose(
                fit.probs[f_lab, 1], fit.labels[f_lab], seed=seed, folds=FOLDS
            )
        else:
            choice = calibration.choose_multiclass(
                fit.probs[f_lab], fit.labels[f_lab], seed=seed, folds=FOLDS
            )
    except ValueError as e:
        return {"not_enough_labels": {"labeled": data.labeled, "detail": str(e)}}
    if not e_lab.any():
        return {
            "not_enough_labels": {
                "labeled": data.labeled,
                "detail": "no labeled items in the eval split",
            }
        }
    cal = choice.calibrator
    raw, calibrated, y = _events(question, ev.probs[e_lab], ev.labels[e_lab], cal)
    if y is not None:
        raw, calibrated = (raw, y), (calibrated, y)
    body = {
        "schema": "fit/1",
        "fit_id": str(fit_id),
        "question_id": question_id,
        "model_served": model_served,
        "type": question.type,
        "created_at": _iso(now),
        "calibration": cal.to_json(),
        "selection": {"cv_folds": FOLDS, "cv_log_loss": choice.scores},
        "provenance": {
            "window": {"from": _iso(now - timedelta(days=window_days)), "to": _iso(now)},
            "seed": seed,
            "split": SPLIT_SHARES,
            "n": {"fit": len(fit.ids), "tune": len(data.tune.ids), "eval": len(ev.ids)},
            "label_horizon_days": horizon_days,
        },
        "metrics": {"raw": _metrics(*raw), "calibrated": _metrics(*calibrated)},
        "reliability": {"raw": _bins(*raw), "calibrated": _bins(*calibrated)},
    }
    tune = data.tune
    return body, _pack(data), [i for i, ok in zip(tune.ids, tune.labeled, strict=True) if ok]


async def run_fit(
    conn: Any, project_id: uuid.UUID, payload: Mapping[str, Any], key_id: uuid.UUID | None = None
) -> dict[str, Any]:
    question_id, model_served = payload["question_id"], payload["model_served"]
    window_days = int(payload.get("window_days", 90))
    settings, _ = await _settings(conn, project_id, question_id)
    now = datetime.now(UTC)
    question, items = await load_items(
        conn, project_id, question_id, model_served, since=now - timedelta(days=window_days)
    )
    fit_id = uuid.uuid4()
    out = await asyncio.to_thread(
        compute_fit,
        question,
        question_id,
        model_served,
        items,
        fit_id=fit_id,
        now=now,
        window_days=window_days,
        horizon_days=int(settings.get("label_horizon_days") or 0),
    )
    if isinstance(out, dict):
        return out
    body, splits, tune_ids = out
    await conn.execute(
        "INSERT INTO fits (fit_id, project_id, question_id, model_served, created_at, body, "
        "splits, label_sources) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb)",
        fit_id,
        project_id,
        question_id,
        model_served,
        now,
        json.dumps(body),
        splits,
        json.dumps(await label_sources(conn, project_id, tune_ids)),
    )
    return {"fit_id": str(fit_id)}


async def label_sources(conn: Any, project_id: uuid.UUID, ids: Sequence[str]) -> list[Any]:
    """Each source and key's share of these decisions' labels (threat T1)."""
    rows = await conn.fetch(
        "SELECT source, key_id, count(*) AS n FROM outcomes "
        "WHERE project_id = $1 AND decision_id = ANY($2::uuid[]) GROUP BY 1, 2 ORDER BY 3 DESC",
        project_id,
        list(ids),
    )
    total = sum(r["n"] for r in rows)
    return [
        {
            "source": r["source"],
            "key_id": str(r["key_id"]) if r["key_id"] else None,
            "share": round(r["n"] / total, 4),
        }
        for r in rows
    ]


# ------------------------------------------------------------------ compiles


def actions_of(question: QuestionIn, settings: Mapping[str, Any]) -> Actions:
    """The question's action mapping (FR-20), with blocked options as indices."""
    raw = settings.get("actions") or {}
    values = [o.value for o in question.options or []]
    blocked = []
    for value in raw.get("block_options") or []:
        found = [i for i, v in enumerate(values) if type(v) is type(value) and v == value]
        if not found:
            raise JobError(f"block_options names {value!r}, which isn't an option")
        blocked.append(found[0])
    return Actions(
        high_means=raw.get("high_means", "block"),
        block_options=tuple(blocked),
        accept_below_level=raw.get("accept_below_level"),
        block_from_level=raw.get("block_from_level"),
    )


def target_of(raw: Mapping[str, Any]) -> Target:
    costs = raw.get("costs")
    try:
        return Target(
            kind=raw["kind"],
            metric=raw.get("metric"),
            alpha=raw.get("alpha"),
            delta=raw.get("delta", 0.05),
            max_escalation=raw.get("max_escalation"),
            costs=Costs(float(costs.get("escalation", 0.0)), dict(costs.get("wrong") or {}))
            if costs
            else None,
        )
    except (KeyError, ValueError) as e:
        raise JobError(f"The target is incomplete: {e}") from None


async def fit_row(
    conn: Any, project_id: uuid.UUID, question_id: str, model_served: str, fit_id: str | None
) -> Any:
    if fit_id:
        row = await conn.fetchrow(
            "SELECT fit_id, body, splits FROM fits WHERE project_id = $1 AND fit_id = $2 "
            "AND question_id = $3 AND model_served = $4",
            project_id,
            uuid.UUID(fit_id),
            question_id,
            model_served,
        )
    else:
        row = await conn.fetchrow(
            "SELECT fit_id, body, splits FROM fits WHERE project_id = $1 AND question_id = $2 "
            "AND model_served = $3 ORDER BY created_at DESC LIMIT 1",
            project_id,
            question_id,
            model_served,
        )
    if row is None:
        raise JobError("No fit for this question and model: run a fit first")
    return row


async def run_compile(
    conn: Any,
    project_id: uuid.UUID,
    payload: Mapping[str, Any],
    key_id: uuid.UUID | None = None,
    *,
    state: str = "draft",
    reason: str | None = None,
    unless_like: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compiles a policy from a fit and stores it in `state`. With `unless_like` (a policy
    body), stores nothing when the result is the same policy: {"unchanged": its id}."""
    question_id, model_served = payload["question_id"], payload["model_served"]
    settings, name = await _settings(conn, project_id, question_id)
    question = (await definitions(conn, project_id, {question_id}))[question_id]
    fit = await fit_row(conn, project_id, question_id, model_served, payload.get("fit_id"))
    body = json.loads(fit["body"])
    target, actions = target_of(payload["target"]), actions_of(question, settings)
    tune, ev = unpack(fit["splits"])
    cal = Calibrator.from_json(body["calibration"])
    window = body["provenance"]["window"]
    try:
        out = await asyncio.to_thread(
            compiler.compile_policy, question.type, cal, tune, ev, actions, target, window=window
        )
    except ValueError as e:
        raise JobError(str(e)) from None
    if not isinstance(out, Compiled):
        report: dict[str, Any] = {"message": out.message}
        if out.needed is not None:
            report["needed"] = out.needed
        if out.binding:
            report |= {"binding": out.binding, "best": out.best}
        return {out.reason: report}
    if unless_like is not None and same_policy(unless_like, cal.method, out.rules):
        return {"unchanged": unless_like["policy_id"]}

    policy_id = uuid.uuid4()
    audit_fraction = float(payload.get("audit_fraction", settings.get("audit_fraction") or 0.0))
    async with conn.transaction():
        version = 1 + await conn.fetchval(
            "SELECT coalesce(max(version), 0) FROM policies "
            "WHERE project_id = $1 AND question_id = $2 AND model_served = $3",
            project_id,
            question_id,
            model_served,
        )
        policy = {
            "schema": "policy/1",
            "policy_id": str(policy_id),
            "question_id": question_id,
            "question_name": name,
            "model_served": model_served,
            "type": question.type,
            "version": version,
            "created_at": _iso(datetime.now(UTC)),
            "fit_id": body["fit_id"],
            "calibration": body["calibration"],
            "rules": [r.to_json() for r in out.rules],
            "fallback_action": "ESCALATE",
            "audit_fraction": audit_fraction,
            "target": target.to_json(),
            "guarantee": out.guarantee,
            "metrics": out.metrics,
        }
        if actions.block_options:
            values = [o.value for o in question.options or []]
            policy["option_actions"] = [values[i] for i in actions.block_options]
        policy["fingerprint"] = fingerprint(policy)
        await conn.execute(
            "INSERT INTO policies (policy_id, project_id, question_id, model_served, version, "
            "state, fit_id, body, fingerprint) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)",
            policy_id,
            project_id,
            question_id,
            model_served,
            version,
            state,
            uuid.UUID(body["fit_id"]),
            json.dumps(policy),
            policy["fingerprint"],
        )
        await conn.execute(  # FR-27: its first event, by whoever asked for it
            "INSERT INTO policy_events (project_id, policy_id, from_state, to_state, key_id, "
            "reason) VALUES ($1, $2, NULL, $3, $4, $5)",
            project_id,
            policy_id,
            state,
            key_id,
            reason,
        )
    return {"policy_id": str(policy_id), "version": version}


SAME_THRESHOLD = 0.005  # a refit that moves no threshold further proposes nothing


def same_policy(policy: Mapping[str, Any], method: str, rules: Sequence[Rule]) -> bool:
    """Whether new rules and a calibration method amount to `policy` again."""
    old = {r["id"]: r for r in policy["rules"]}
    return (
        policy["calibration"]["method"] == method
        and old.keys() == {r.id for r in rules}
        and all(
            old[r.id]["action"] == r.action
            and abs(old[r.id]["threshold"] - r.threshold) < SAME_THRESHOLD
            for r in rules
        )
    )


# ------------------------------------------------------------------ the simulator

SIMULATE_GRID = 50


def rule_of(raw: Mapping[str, Any]) -> Rule:
    scalar = raw["scalar"]
    return Rule(
        raw["id"],
        raw["action"],
        scalar["tail_ge"] if isinstance(scalar, dict) else scalar,
        raw["op"],
        float(raw["threshold"]),
    )


def simulate_rows(
    question: QuestionIn,
    cal: Calibrator,
    evaluation: Split,
    actions: Actions,
    candidates: Sequence[Sequence[Rule]] | None,
    costs: Costs | None,
) -> list[dict[str, Any]]:
    """FR-26: metrics on the eval split for each candidate rule set; without candidates,
    a grid of thresholds per rule (50 for one rule, 22 x 22 for two)."""
    scored = compiler.score(question.type, cal, evaluation, actions)
    if candidates is None:
        shapes = compiler.templates(question.type, actions)
        size = SIMULATE_GRID if len(shapes) == 1 else 22
        grids = [
            np.unique(np.quantile(scored.values[s.scalar], np.linspace(0, 1, size)))
            if scored.n
            else np.array([0.5])
            for s in shapes
        ]
        mesh = np.array(np.meshgrid(*grids, indexing="ij")).reshape(len(shapes), -1).T
        candidates = [
            [
                Rule(s.id, s.action, s.scalar, s.op, float(t))
                for s, t in zip(shapes, row, strict=True)
            ]
            for row in mesh
        ]
    rows = []
    for rules in candidates:
        for rule in rules:
            if rule.scalar not in scored.values:
                raise JobError(f"rule {rule.id} reads a scalar this question doesn't have")
        rows.append(
            {
                "rules": [r.to_json() for r in rules],
                "metrics": compiler.simulate(rules, scored, actions, costs),
            }
        )
    return rows
