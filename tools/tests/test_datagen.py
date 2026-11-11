import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from trimset.hashing import question_id
from trimset_server.records import OutcomeIn, QuestionIn, RecordIn, fits
from trimset_tools import datagen
from trimset_tools.datagen import SCENARIOS, Plants, generate

END = datetime(2026, 11, 1, tzinfo=UTC)


def _ece(data: datagen.Dataset, bins: int = 10) -> float:
    """Expected calibration error of the noul probability against the true label."""
    pairs = [
        (r["probabilities"][0], bool(t["label"]))
        for r, t in zip(data.records, data.truth, strict=True)
    ]
    total = 0.0
    for b in range(bins):
        inside = [
            (p, y) for p, y in pairs if b / bins <= p < (b + 1) / bins or (b == bins - 1 and p == 1)
        ]
        if inside:
            mean_p = sum(p for p, _ in inside) / len(inside)
            rate = sum(y for _, y in inside) / len(inside)
            total += len(inside) / len(pairs) * abs(mean_p - rate)
    return total


@pytest.mark.req("FR-60")
@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_every_scenario_makes_valid_records_and_outcomes(key: str) -> None:
    data = generate(key, seed=1, end=END, days=10, per_day=100)
    question = QuestionIn.model_validate(data.definition)
    assert question_id(data.definition) == data.question_id
    assert len(data.records) == len(data.truth) == 1000
    for record in data.records:
        r = RecordIn.model_validate(record)
        assert r.question_id == data.question_id
        assert len(r.probabilities or []) == question.size()
        assert abs(sum(r.probabilities or []) - 1) < 1e-3 or question.size() == 1
    assert data.outcomes, "every scenario sends some labels back"
    for arrives, outcome in data.outcomes:
        o = OutcomeIn.model_validate(outcome)
        assert fits(question, o.label)
        assert arrives == o.observed_at
    ids = [r["decision_id"] for r in data.records]
    assert len(set(ids)) == len(ids)
    assert [r["ts"] for r in data.records] == sorted(r["ts"] for r in data.records)


@pytest.mark.req("FR-60")
def test_the_same_seed_gives_the_same_data_and_another_seed_other_data() -> None:
    def make(seed: int) -> Any:
        data = generate("ticket-triage", seed=seed, end=END, days=2, per_day=50)
        return data.records, data.outcomes, data.truth

    assert make(3) == make(3)
    assert make(3)[0] != make(4)[0]


@pytest.mark.req("FR-60")
def test_an_unplanted_model_is_calibrated_and_a_planted_one_is_not() -> None:
    calibrated = generate("refund-fraud", seed=1, end=END, days=100, per_day=200)
    planted = generate(
        "refund-fraud", seed=1, end=END, days=100, per_day=200, plants=Plants(slope=0.5, bias=1)
    )
    assert _ece(calibrated) <= 0.02
    assert _ece(planted) >= 0.08  # SM-1's starting point


@pytest.mark.req("FR-60")
def test_drift_makes_the_tilt_class_more_likely_from_its_point_on() -> None:
    data = generate(
        "refund-fraud", seed=2, end=END, days=100, per_day=200, plants=Plants(drift_at=0.5, drift=4)
    )
    half = len(data.truth) // 2
    before = sum(t["label"] for t in data.truth[: half - 500]) / (half - 500)
    after = sum(t["label"] for t in data.truth[half + 500 :]) / (len(data.truth) - half - 500)
    assert 0.02 < before < 0.04
    assert after > 2.5 * before


@pytest.mark.req("FR-60")
def test_a_silent_swap_changes_the_served_model_but_not_the_requested_one() -> None:
    data = generate(
        "ticket-triage", seed=1, end=END, days=10, per_day=100, plants=Plants(swap_at=0.6)
    )
    served = [r["model_served"] for r in data.records]
    first_new = served.index("jev-1.15.0")
    assert set(served[:first_new]) == {"jev-1.14.0"}
    assert set(served[first_new:]) == {"jev-1.15.0"}
    assert 550 < first_new < 650
    assert {r["model_requested"] for r in data.records} == {"jev-latest"}


@pytest.mark.req("FR-60")
def test_refund_labels_arrive_late_and_held_refunds_learn_nothing() -> None:
    data = generate("refund-fraud", seed=5, end=END, days=200, per_day=100)
    by_ref = {r["external_ref"]: r for r in data.records}
    delays: dict[str, list[float]] = {"downstream": [], "human_review": []}
    for arrives, o in data.outcomes:
        record = by_ref[o["external_ref"]]
        assert record["action_taken"] != "BLOCK"
        ts = datetime.fromisoformat(record["ts"])
        delays[o["source"]].append((arrives - ts).total_seconds() / 86400)
    assert (
        delays["downstream"] and 30 <= min(delays["downstream"]) <= max(delays["downstream"]) <= 90
    )
    assert max(delays["human_review"]) <= 2

    audited = [r for r in data.records if r["audit"]]
    accepted = [r for r in data.records if r["action_taken"] == "ACCEPT"]
    assert 0.005 < len(audited) / (len(audited) + len(accepted)) < 0.02
    assert {(r["action_taken"], r["action_source"]) for r in audited} == {("ESCALATE", "audit")}


@pytest.mark.req("FR-60")
def test_missing_labels_and_slower_delays() -> None:
    def outcomes(**plants: Any) -> list[Any]:
        return generate(
            "ticket-triage", seed=1, end=END, days=5, per_day=100, plants=Plants(**plants)
        ).outcomes

    assert outcomes(missing=1.0) == []
    some = len(outcomes(missing=0.5))
    assert 0.4 < some / len(outcomes()) < 0.6

    def mean_delay(data: list[Any], records: list[Any]) -> float:
        ts = {r["external_ref"]: datetime.fromisoformat(r["ts"]) for r in records}
        return sum((t - ts[o["external_ref"]]).total_seconds() for t, o in data) / len(data)

    normal = generate("ticket-triage", seed=1, end=END, days=5, per_day=100)
    slow = generate(
        "ticket-triage", seed=1, end=END, days=5, per_day=100, plants=Plants(delay_scale=2)
    )
    ratio = mean_delay(slow.outcomes, slow.records) / mean_delay(normal.outcomes, normal.records)
    assert ratio == pytest.approx(2, rel=0.01)


@pytest.mark.parametrize(
    "plants",
    [
        {"drift_at": 1.5},
        {"swap_at": -0.1},
        {"missing": 2},
        {"slope": 0},
        {"drift": 0},
        {"delay_scale": 0},
        {"swap_to": (0, 1)},
    ],
)
def test_impossible_plants_are_refused(plants: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Plants(**plants)


def test_end_needs_a_time_zone() -> None:
    with pytest.raises(ValueError, match="time zone"):
        generate("refund-fraud", seed=1, end=datetime(2026, 11, 1))


def test_jsonl_files(tmp_path: Path) -> None:
    data = generate("model-routing", seed=1, end=END, days=1, per_day=50)
    datagen.write_jsonl(data, tmp_path)
    assert json.loads((tmp_path / "questions.json").read_text()) == {
        data.question_id: data.definition
    }
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert records == data.records
    outcomes = (tmp_path / "outcomes.jsonl").read_text().splitlines()
    assert len(outcomes) == len(data.outcomes)
    assert "arrives_at" in json.loads(outcomes[0])
    assert len((tmp_path / "truth.jsonl").read_text().splitlines()) == 50


def _fake_server() -> tuple[httpx2.Client, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/decisions:log":
            return httpx2.Response(
                200, json={"stored": len(body["records"]), "duplicates": 0, "rejected": 0}
            )
        if request.url.path == "/v1/outcomes":
            n = len(body["outcomes"])
            return httpx2.Response(200, json={"matched": n, "held": 0, "rejected": 0})
        return httpx2.Response(200, json={})

    client = httpx2.Client(base_url="http://trimset.test", transport=httpx2.MockTransport(answer))
    return client, seen


@pytest.mark.req("FR-60")
def test_send_logs_decisions_then_the_outcomes_that_have_arrived() -> None:
    data = generate("refund-fraud", seed=1, end=END, days=10, per_day=120)
    client, seen = _fake_server()
    totals = datagen.send(data, client, "tsi_x", admin_key="tsa_y", now=END)
    arrived = sum(t <= END for t, _ in data.outcomes)
    assert totals["stored"] == 1200
    assert totals["matched"] == arrived
    assert totals["not_yet_arrived"] == len(data.outcomes) - arrived > 0  # chargebacks
    paths = [r.url.path for r in seen]
    assert paths[:3] == ["/v1/decisions:log"] * 3  # 500 per batch
    assert paths[3] == "/v1/admin/questions/refund_fraud"
    assert seen[3].headers["authorization"] == "Bearer tsa_y"
    assert json.loads(seen[3].content) == {
        "settings": {"default_rule": {"after_days": 120, "label": False}}
    }
    assert set(paths[4:]) == {"/v1/outcomes"}


def test_send_without_an_admin_key_leaves_the_rules_alone() -> None:
    data = generate("refund-fraud", seed=1, end=END, days=1, per_day=10)
    client, seen = _fake_server()
    datagen.send(data, client, "tsi_x", now=END + timedelta(days=365))
    assert "/v1/admin/questions/refund_fraud" not in [r.url.path for r in seen]


def test_the_command_line_writes_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    datagen.main(
        ["pii-guardrail", "--days", "1", "--per-day", "20", "--end", "2026-11-01T00:00:00Z",
         "--out", str(tmp_path)]
    )  # fmt: skip
    assert json.loads(capsys.readouterr().out)["records"] == 20
    assert (tmp_path / "records.jsonl").exists()


def test_the_command_line_sends_with_keys_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, seen = _fake_server()
    monkeypatch.setattr(datagen.httpx2, "Client", lambda **_: client)
    monkeypatch.setenv("TRIMSET_INGEST_KEY", "tsi_env")
    monkeypatch.delenv("TRIMSET_ADMIN_KEY", raising=False)
    datagen.main(["tool-approval", "--days", "1", "--per-day", "10", "--url", "http://x"])
    assert json.loads(capsys.readouterr().out)["stored"] == 10
    assert seen[0].headers["authorization"] == "Bearer tsi_env"


@pytest.mark.parametrize(
    "argv",
    [
        ["refund-fraud"],  # neither --out nor --url
        ["refund-fraud", "--url", "http://x"],  # no key
        ["refund-fraud", "--out", "x", "--end", "2026-11-01T00:00:00"],  # no time zone
        ["refund-fraud", "--out", "x", "--missing", "2"],
        ["no-such-scenario", "--out", "x"],
    ],
)
def test_the_command_line_refuses_bad_arguments(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRIMSET_INGEST_KEY", raising=False)
    with pytest.raises(SystemExit):
        datagen.main(argv)
