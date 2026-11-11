"""Which labeled decisions a fit uses, and how they split (FR-14, FR-18, FR-19, ADR 0006).

The rules, for one group (question, served model, type; never pooled, FR-15):
- **Window:** decisions from the last `window_days` (90 by default).
- **Matured:** older than the question's label horizon, so slow outcomes had time to come.
- **Labeled:** an outcome exists. Refusals and shadows never reach here (the loader skips
  them).
- **One per reference:** where `external_ref` is set, only the latest decision for it.
- **Split** 40 / 40 / 20 into fit, tune and eval by a hash of (seed, decision_id): the
  same item lands in the same split every time, and a fit never sees tune or eval items.
- **Minimum:** 200 labeled items by default; below it, how many more are needed.

`coverage` reports, for each action path, how many decisions got a label and how late,
and warns where a path gets almost none: blocked refunds never see a chargeback.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

DAY = 86_400.0
SPLITS = ("fit", "tune", "eval")
_CUTS = (0.4, 0.8)  # fit below 0.4, tune below 0.8, eval above
STARVED_SHARE = 0.02  # a path with fewer labels than this gets a warning


@dataclass(frozen=True)
class Item:
    """One decision of a group, as the loader reads it. `probs` is over the classes in
    question order (noul: no, yes); `label` is the true class's index, or None."""

    decision_id: str
    ts: float  # seconds since the epoch
    probs: tuple[float, ...]
    label: int | None = None
    labelled_at: float | None = None  # when the outcome was observed
    action: str | None = None
    audit: bool = False
    external_ref: str | None = None


def split_of(decision_id: str, seed: int) -> str:
    """fit, tune or eval: a pure function of the decision and the seed (FR-18)."""
    digest = hashlib.sha256(f"{seed}:{decision_id}".encode()).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64
    return SPLITS[0] if u < _CUTS[0] else SPLITS[1] if u < _CUTS[1] else SPLITS[2]


@dataclass(frozen=True)
class Split:
    ids: tuple[str, ...]
    probs: npt.NDArray[np.float64]  # (n, k)
    labels: npt.NDArray[np.intp]  # (n,)


@dataclass(frozen=True)
class Dataset:
    """The split items, and how many more labels are needed (0 when there are enough)."""

    fit: Split
    tune: Split
    eval: Split
    labeled: int
    needed: int
    dropped: dict[str, int] = field(default_factory=dict)  # why items were left out

    def check_unseen(self, ids: Iterable[str]) -> None:
        """Refuses items the fit was trained on (FR-18)."""
        seen = set(self.fit.ids).intersection(ids)
        if seen:
            raise ValueError(f"{len(seen)} items were used to fit and can't be used again")


def _split(items: Sequence[Item], k: int) -> Split:
    probs = np.array([i.probs for i in items], dtype=np.float64).reshape(len(items), k)
    labels = np.array([i.label for i in items], dtype=np.intp)
    return Split(tuple(i.decision_id for i in items), probs, labels)


def build(
    items: Iterable[Item],
    *,
    now: float,
    seed: int,
    window_days: float = 90.0,
    horizon_days: float = 0.0,
    minimum: int = 200,
) -> Dataset:
    """The fit, tune and eval items for one group."""
    start, matured_by = now - window_days * DAY, now - horizon_days * DAY
    dropped: dict[str, int] = defaultdict(int)
    latest: dict[str, Item] = {}
    kept: list[Item] = []
    sizes: set[int] = set()
    for item in items:
        if item.ts < start:
            dropped["outside the window"] += 1
        elif item.ts > matured_by:
            dropped["not matured"] += 1
        elif item.label is None:
            dropped["no label"] += 1
        elif item.external_ref is None:
            kept.append(item)
        else:
            held = latest.get(item.external_ref)
            if held is not None:
                dropped["an older decision for the same external_ref"] += 1
            if held is None or (item.ts, item.decision_id) > (held.ts, held.decision_id):
                latest[item.external_ref] = item
        sizes.add(len(item.probs))
    kept += latest.values()
    if len(sizes) > 1:
        raise ValueError("items of one group must have the same number of classes")
    k = sizes.pop() if sizes else 0
    kept.sort(key=lambda i: (i.ts, i.decision_id))
    by_split: dict[str, list[Item]] = {s: [] for s in SPLITS}
    for item in kept:
        by_split[split_of(item.decision_id, seed)].append(item)
    return Dataset(
        fit=_split(by_split["fit"], k),
        tune=_split(by_split["tune"], k),
        eval=_split(by_split["eval"], k),
        labeled=len(kept),
        needed=max(0, minimum - len(kept)),
        dropped=dict(dropped),
    )


@dataclass(frozen=True)
class PathCoverage:
    """Labels on one action path (FR-14). `median_delay_s` is None without labels."""

    action: str
    decisions: int
    labeled: int
    audited: int
    median_delay_s: float | None
    starved: bool  # almost no labels: errors on this path can't be measured

    @property
    def share(self) -> float:
        return self.labeled / self.decisions


def coverage(
    items: Iterable[Item], *, now: float, window_days: float = 90.0, horizon_days: float = 0.0
) -> list[PathCoverage]:
    """For each action path, in the window and matured: decisions, labels and delay."""
    start, matured_by = now - window_days * DAY, now - horizon_days * DAY
    paths: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        if start <= item.ts <= matured_by:
            paths[item.action or "unknown"].append(item)
    out = []
    for action in sorted(paths):
        group = paths[action]
        delays = [
            i.labelled_at - i.ts for i in group if i.label is not None and i.labelled_at is not None
        ]
        labeled = sum(i.label is not None for i in group)
        out.append(
            PathCoverage(
                action=action,
                decisions=len(group),
                labeled=labeled,
                audited=sum(i.audit for i in group),
                median_delay_s=float(np.median(delays)) if delays else None,
                starved=labeled / len(group) < STARVED_SHARE,
            )
        )
    return out
