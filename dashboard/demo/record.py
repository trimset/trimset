"""Record what the dashboard asks a real server, for the public demo (ADR 0017).

CI runs this after the M2 demo (examples/m2-refund-fraud), against that server:

    python3 dashboard/demo/record.py --url http://127.0.0.1:8800 --out recorded.json

The admin key comes from TRIMSET_KEY. Every request is one the dashboard makes, so a
failure here is also a dashboard call the real server refuses. The demo build answers
from the file and computes nothing: the simulator's numbers are this server's, on a
0.05 grid of thresholds.
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

GRID = [round(i * 0.05, 2) for i in range(21)]
COSTS = {"escalation": 1.2, "wrong": {"ACCEPT": 85, "BLOCK": 6}}
ALPHAS = (0.01, 0.02, 0.05)


class Api:
    def __init__(self, url: str, key: str) -> None:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise SystemExit(f"--url must start with http:// or https://, not {url!r}")
        self.url, self.key = url.rstrip("/"), key

    def __call__(self, path: str, body: Any = None, **query: Any) -> Any:
        if query:
            path += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(  # noqa: S310 - http(s) only, checked in __init__
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as res:  # noqa: S310 - as above
            return json.load(res)

    def job(self, job: dict[str, Any]) -> dict[str, Any]:
        for _ in range(240):
            if job["status"] in ("succeeded", "failed"):
                return job
            time.sleep(0.5)
            job = self(f"/v1/admin/jobs/{job['job_id']}")
        raise TimeoutError(f"job {job['job_id']} still {job['status']}")


def record(api: Api, name: str) -> dict[str, Any]:
    q = urllib.parse.quote(name, safe="")
    question = api(f"/v1/admin/questions/{q}")
    model = max(question["models"], key=lambda m: m["first_seen"])["model_served"]
    fit = api(f"/v1/admin/questions/{q}/fits", model_served=model)["items"][0]
    shapes = api(
        f"/v1/admin/questions/{q}/simulate", {"model_served": model, "fit_id": fit["fit_id"]}
    )["rows"][0]["rules"]
    if sorted(r["op"] for r in shapes) != ["ge", "lt"]:
        raise SystemExit(f"the demo expects a noul question with two rules, got {shapes}")

    # Every (lo, hi) pair on the grid: the lt rule takes lo, the ge rule takes hi.
    pairs = [(lo, hi) for lo in GRID for hi in GRID if lo < hi]
    candidates = [
        [{**r, "threshold": lo if r["op"] == "lt" else hi} for r in shapes] for lo, hi in pairs
    ]
    rows = api(
        f"/v1/admin/questions/{q}/simulate",
        {"model_served": model, "fit_id": fit["fit_id"], "candidates": candidates, "costs": COSTS},
    )["rows"]
    grid = {f"{lo:.2f},{hi:.2f}": row["metrics"] for (lo, hi), row in zip(pairs, rows, strict=True)}

    compiles = {}
    for alpha in ALPHAS:
        target = {"kind": "risk", "metric": "false_pass", "alpha": alpha, "delta": 0.05}
        body = {"model_served": model, "fit_id": fit["fit_id"], "target": target}
        compiles[f"{alpha:g}"] = api.job(api(f"/v1/admin/questions/{q}/compile", body))

    # Walk the lifecycle (FR-27): the 2% policy goes live, the 1% one waits for approval.
    def move(job: dict[str, Any], *actions: str) -> None:
        policy_id = (job.get("result") or {}).get("policy_id")
        for action in actions if policy_id else ():
            note = {"reason": "recorded for the demo"}
            api(f"/v1/admin/policies/{policy_id}/transitions", {"action": action, **note})

    move(compiles["0.02"], "submit", "approve", "activate")
    move(compiles["0.01"], "submit")

    policies = api("/v1/admin/policies", question=name)["items"]
    ids = [p["policy"]["policy_id"] for p in policies]
    diffs = {f"{a}:": api(f"/v1/admin/policies/{a}/diff") for a in ids}
    diffs |= {
        f"{a}:{b}": api(f"/v1/admin/policies/{a}/diff", against=b)
        for a in ids
        for b in ids
        if a != b
    }

    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": api("/v1/admin/project"),
        "keys": api("/v1/admin/keys"),
        "question": question,
        "model": model,
        "fits": [fit],
        "coverage": api(f"/v1/admin/questions/{q}/coverage", model_served=model),
        "shapes": shapes,
        "costs": COSTS,
        "grid": grid,
        "compiles": compiles,
        "policies": policies,
        "diffs": diffs,
        "decisions": api("/v1/admin/decisions", limit=200)["items"],
        "pending": [
            d["decision_id"]
            for d in api("/v1/admin/decisions", limit=200, outcome="pending")["items"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8800")
    parser.add_argument("--question", default="refund_fraud")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    data = record(Api(args.url, os.environ["TRIMSET_KEY"]), args.question)
    with open(args.out, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"{args.out}: {len(data['grid'])} simulated pairs, {len(data['policies'])} policies")


if __name__ == "__main__":
    main()
