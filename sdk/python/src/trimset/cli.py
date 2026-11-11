"""The trimset CLI (FR-55): calibrate, simulate and compile policies, read them, and watch
coverage and decisions on a trimset server. Readable tables by default; JSON with --json.

    trimset config set --url https://trimset.example.com       # then asks for the key
    trimset calibrate refund_fraud --model jev-1.14.0
    trimset simulate refund_fraud --model jev-1.14.0 --lo 0.30 --hi 0.85
    trimset policy compile refund_fraud --model jev-1.14.0 --false-pass 0.02 --max-escalation 0.15
    trimset policy show --question refund_fraud --model jev-1.14.0
    trimset coverage refund_fraud
    trimset decisions tail --question refund_fraud

The key comes from TRIMSET_KEY, then the config file (written with mode 0600). Standard
library only, like the rest of the SDK.
"""

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

JOB_TIMEOUT_S = 600.0


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "trimset" / "config.json"


def load_config() -> dict[str, str]:
    path = config_path()
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    url = os.environ.get("TRIMSET_URL") or saved.get("url")
    key = os.environ.get("TRIMSET_KEY") or saved.get("key")
    return {k: v for k, v in (("url", url), ("key", key)) if v}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The API never redirects; following one would send the key wherever it points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_open = urllib.request.build_opener(_NoRedirect).open


def printable(text: Any) -> str:
    """Text with control characters escaped, so a value stored on the server (an escape
    sequence in an external_ref, say) can't drive the terminal."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in str(text))


def say(text: Any = "", file: Any = None) -> None:
    """Every line the CLI writes goes through here."""
    print("\n".join(map(printable, str(text).split("\n"))), file=file or sys.stdout)


class Client:
    """The API over urllib: JSON in and out, a Problem turned into a CliError."""

    def __init__(self, url: str, key: str, timeout: float = 30.0) -> None:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise CliError(f"the server URL must start with http:// or https://, not {url!r}")
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(  # noqa: S310 - http(s) only, checked in __init__
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": "trimset-cli",
            },
        )
        try:
            with _open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise CliError(
                    f"{self.url} answered with a redirect ({e.code}); use the server's own URL"
                ) from None
            raise CliError(_problem(e)) from None
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise CliError(f"can't reach {self.url}: {reason}") from None
        return json.loads(raw) if raw else None

    def wait(self, job: dict[str, Any]) -> dict[str, Any]:
        """Polls a job until it finishes."""
        deadline = time.monotonic() + JOB_TIMEOUT_S
        while job["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise CliError(f"job {job['job_id']} is still {job['status']}; check it later")
            time.sleep(0.5)
            job = self.call("GET", f"/v1/admin/jobs/{job['job_id']}")
        if job["status"] == "failed":
            raise CliError(f"job {job['job_id']} failed: {job.get('error')}")
        return job


def _problem(e: urllib.error.HTTPError) -> str:
    try:
        problem = json.loads(e.read() or b"{}")
    except ValueError:
        problem = {}
    text = f"{problem.get('title') or e.reason} ({e.code})"
    if problem.get("detail"):
        text += f": {problem['detail']}"
    for error in problem.get("errors") or []:
        text += f"\n  {error.get('pointer', '')} {error.get('message', '')}".rstrip()
    return text


# ------------------------------------------------------------------ printing


def pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [list(map(printable, header)), *[list(map(printable, r)) for r in rows]]  # widths
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    lines = [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in cells
    ]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def scalar_name(scalar: Any) -> str:
    if isinstance(scalar, dict):
        return f"P(level >= {scalar['tail_ge']})"
    return {"p": "p", "top": "top option"}.get(scalar, str(scalar))


def describe_rule(rule: dict[str, Any]) -> str:
    op = "<" if rule["op"] == "lt" else ">="
    return f"{rule['action']} when {scalar_name(rule['scalar'])} {op} {rule['threshold']:.4f}"


def metrics_rows(name: str, m: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for rule, r in m["rules"].items():
        ci = r["ci95"]
        rows.append(
            [
                name,
                rule,
                r["fired"],
                pct(r["rate"], 2),
                f"[{pct(ci[0], 2)}, {pct(ci[1], 2)}]" if ci else "-",
                r["unlabeled"],
            ]
        )
    return rows


def print_metrics(by_split: dict[str, dict[str, Any]]) -> None:
    rows = [
        [
            name,
            m["n"],
            pct(m["automated"]),
            pct(m["escalated"]),
            num(m.get("expected_cost_per_1000"), 2),
        ]
        for name, m in by_split.items()
        if m is not None
    ]
    say(table(["split", "n", "automated", "escalated", "cost/1000"], rows))
    detail = [row for name, m in by_split.items() if m is not None for row in metrics_rows(name, m)]
    if detail:
        say()
        say(table(["split", "rule", "fired", "wrong", "95% interval", "unlabeled"], detail))


def print_fit(fit: dict[str, Any], name: str | None = None) -> None:
    cal = fit["calibration"]
    name = name or fit["question_id"]
    say(f"{name} on {fit['model_served']}: {cal['method']} (fit {fit['fit_id']})")
    scores = ", ".join(f"{k} {v:.4f}" for k, v in fit["selection"]["cv_log_loss"].items())
    say(f"chosen by {fit['selection']['cv_folds']}-fold log loss: {scores}")
    raw, calibrated = fit["metrics"]["raw"], fit["metrics"]["calibrated"]
    say()
    say(
        table(
            ["eval split", "raw", "calibrated"],
            [
                ["log loss", num(raw["log_loss"]), num(calibrated["log_loss"])],
                ["Brier", num(raw["brier"]), num(calibrated["brier"])],
                [
                    "ECE (equal width)",
                    num(raw["ece_equal_width"]),
                    num(calibrated["ece_equal_width"]),
                ],
                ["ECE (equal mass)", num(raw["ece_equal_mass"]), num(calibrated["ece_equal_mass"])],
                ["items", raw["n"], calibrated["n"]],
            ],
        )
    )
    say()
    say("Reliability, calibrated:")
    say(
        table(
            ["predicted", "items", "mean", "observed", "95% interval"],
            [
                [
                    f"{b['lo']:.2f}-{b['hi']:.2f}",
                    b["count"],
                    num(b["mean_predicted"], 3),
                    num(b["observed"], 3),
                    f"[{num(b['ci95'][0], 3)}, {num(b['ci95'][1], 3)}]",
                ]
                for b in fit["reliability"]["calibrated"]
            ],
        )
    )


def print_policy(view: dict[str, Any]) -> None:
    p = view["policy"]
    name = p.get("question_name") or p["question_id"]
    say(f"v{p['version']} {view['state']}: {name} on {p['model_served']} (policy {p['policy_id']})")
    say(f"calibration: {p['calibration']['method']}; fingerprint {p['fingerprint']}")
    for rule in p["rules"]:
        say(f"  {describe_rule(rule)}")
    if p.get("option_actions"):
        say(f"  options that BLOCK instead: {', '.join(map(str, p['option_actions']))}")
    say(f"  otherwise {p['fallback_action']}")
    g = p["guarantee"]
    if g["status"] == "certified":
        n = ", ".join(f"{k} {v}" for k, v in g["n"].items())
        say(f"guarantee ({view['guarantee_status']}): {g['statement']} (items: {n})")
    else:
        say(f"no guarantee: {g['reason']}. {g.get('detail', '')}".rstrip())
    say()
    print_metrics(p["metrics"])


# ------------------------------------------------------------------ commands


def _job_result(client: Client, job: dict[str, Any]) -> dict[str, Any]:
    result = client.wait(job)["result"]
    for reason in ("not_enough_labels", "infeasible"):
        if reason in result:
            report = result[reason]
            message = report.get("message") or report.get("detail")
            if message is None:
                message = (
                    f"{report.get('labeled', 0)} labeled, {report.get('needed', '?')} more needed"
                )
            raise CliError(f"{reason.replace('_', ' ')}: {message}", code=2)
    return result  # type: ignore[no-any-return]


def cmd_calibrate(client: Client, args: argparse.Namespace) -> None:
    body = {"model_served": args.model, "window_days": args.window_days}
    job = client.call("POST", f"/v1/admin/questions/{_q(args.question)}/fits", body)
    result = _job_result(client, job)
    fit = client.call("GET", f"/v1/admin/fits/{result['fit_id']}")
    if args.json:
        say(json.dumps(fit, indent=2))
    else:
        print_fit(fit, args.question)


def _rules(question: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]] | None:
    actions = question["settings"].get("actions") or {}
    kind = question["type"]
    rules: list[dict[str, Any]] = []
    if kind == "noul":
        block_high = actions.get("high_means", "block") == "block"
        if args.lo is not None:
            action = "ACCEPT" if block_high else "BLOCK"
            rules.append(_rule("accept" if block_high else "block", action, "p", "lt", args.lo))
        if args.hi is not None:
            action = "BLOCK" if block_high else "ACCEPT"
            rules.append(_rule("block" if block_high else "accept", action, "p", "ge", args.hi))
        rules.sort(key=lambda r: r["id"] != "accept")
    elif kind == "choice":
        if args.t is not None:
            rules.append(_rule("select", "SELECT", "top", "ge", args.t))
    else:
        for value, level, rid, action, op in (
            (args.lo, actions.get("accept_below_level"), "accept", "ACCEPT", "lt"),
            (args.hi, actions.get("block_from_level"), "block", "BLOCK", "ge"),
        ):
            if value is None:
                continue
            if level is None:
                raise CliError(
                    f"--{'lo' if op == 'lt' else 'hi'} needs a level: set the question's actions"
                )
            rules.append(_rule(rid, action, {"tail_ge": level}, op, value))
    return rules or None


def _rule(rid: str, action: str, scalar: Any, op: str, threshold: float) -> dict[str, Any]:
    return {"id": rid, "action": action, "scalar": scalar, "op": op, "threshold": threshold}


def _costs(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.cost_escalation is None:
        return None
    wrong = {}
    for item in args.cost_wrong or []:
        action, _, value = item.partition("=")
        try:
            wrong[action.upper()] = float(value)
        except ValueError:
            raise CliError(f"--cost-wrong takes ACTION=COST, not {item!r}") from None
    return {"escalation": args.cost_escalation, "wrong": wrong}


def cmd_simulate(client: Client, args: argparse.Namespace) -> None:
    question = client.call("GET", f"/v1/admin/questions/{_q(args.question)}")
    rules = _rules(question, args)
    body: dict[str, Any] = {"model_served": args.model}
    if rules:
        body["candidates"] = [rules]
    if args.fit:
        body["fit_id"] = args.fit
    if costs := _costs(args):
        body["costs"] = costs
    out = client.call("POST", f"/v1/admin/questions/{_q(args.question)}/simulate", body)
    if args.json:
        say(json.dumps(out, indent=2))
        return
    rows = sorted(out["rows"], key=lambda r: -r["metrics"]["automated"])[: args.top]
    say(f"on the eval split of fit {out['fit_id']}:")
    shown = []
    for row in rows:
        m = row["metrics"]
        rates = "; ".join(f"{rid} {pct(r['rate'], 2)}" for rid, r in m["rules"].items())
        shown.append(
            [
                "; ".join(describe_rule(r) for r in row["rules"]),
                pct(m["automated"]),
                pct(m["escalated"]),
                rates,
                num(m.get("expected_cost_per_1000"), 2),
            ]
        )
    say(table(["rules", "automated", "escalated", "error rate", "cost/1000"], shown))


def cmd_compile(client: Client, args: argparse.Namespace) -> None:
    chosen = [
        x for x in (args.false_pass, args.error_budget, args.cost_escalation) if x is not None
    ]
    if len(chosen) != 1:
        raise CliError("give exactly one of --false-pass, --error-budget or --cost-escalation")
    if args.cost_escalation is not None:
        target: dict[str, Any] = {"kind": "cost", "costs": _costs(args)}
    else:
        metric = "false_pass" if args.false_pass is not None else "error_budget"
        alpha = args.false_pass if args.false_pass is not None else args.error_budget
        target = {
            "kind": "risk",
            "metric": metric,
            "alpha": alpha,
            "delta": round(1 - args.confidence, 10),
        }
    if args.max_escalation is not None:
        target["max_escalation"] = args.max_escalation
    body: dict[str, Any] = {"model_served": args.model, "target": target}
    if args.fit:
        body["fit_id"] = args.fit
    if args.audit_fraction is not None:
        body["audit_fraction"] = args.audit_fraction
    job = client.call("POST", f"/v1/admin/questions/{_q(args.question)}/compile", body)
    result = _job_result(client, job)
    view = client.call("GET", f"/v1/admin/policies/{result['policy_id']}")
    if args.json:
        say(json.dumps(view, indent=2))
    else:
        print_policy(view)


def cmd_show(client: Client, args: argparse.Namespace) -> None:
    if args.policy_id:
        view = client.call("GET", f"/v1/admin/policies/{args.policy_id}")
    else:
        if not args.question:
            raise CliError("give a policy id, or --question (and --model)")
        params = {"question": args.question, "model_served": args.model, "limit": 1}
        items = client.call("GET", "/v1/admin/policies", params=params)["items"]
        if not items:
            raise CliError(f"no policy for {args.question} yet: run trimset policy compile", code=2)
        view = items[0]
    if args.json:
        say(json.dumps(view, indent=2))
    else:
        print_policy(view)


def cmd_diff(client: Client, args: argparse.Namespace) -> None:
    diff = client.call(
        "GET", f"/v1/admin/policies/{args.policy_id}/diff", params={"against": args.against}
    )
    if args.json:
        say(json.dumps(diff, indent=2))
        return
    against = diff["against"] or "nothing (no active policy)"
    say(f"policy {diff['policy_id']} against {against}:")
    say(
        table(
            ["rule", "before", "after"],
            [[r["id"], num(r["before"]), num(r["after"])] for r in diff["rules"]],
        )
    )
    say()
    print_metrics({"before": diff["metrics"]["before"], "after": diff["metrics"]["after"]})


def cmd_coverage(client: Client, args: argparse.Namespace) -> None:
    out = client.call(
        "GET",
        f"/v1/admin/questions/{_q(args.question)}/coverage",
        params={"model_served": args.model},
    )
    if args.json:
        say(json.dumps(out, indent=2))
        return
    rows = [
        [
            p["model_served"],
            p["action"],
            p["decisions"],
            p["matured"],
            f"{p['labeled']} ({pct(p['labeled'] / p['decisions'] if p['decisions'] else None)})",
            p.get("audited_labeled", 0),
            num(p.get("median_delay_hours"), 1),
        ]
        for p in out["paths"]
    ]
    say(
        table(
            ["model", "action", "decisions", "matured", "labeled", "audited", "median delay h"],
            rows,
        )
    )
    starved = [p for p in out["paths"] if p["decisions"] and p["labeled"] / p["decisions"] < 0.02]
    for p in starved:
        say(
            f"warning: {p['action']} on {p['model_served']} has almost no labels, "
            "so its errors can't be measured"
        )


def cmd_tail(client: Client, args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"question": args.question, "model_served": args.model, "limit": 20}
    seen: str | None = None
    while True:
        items = client.call("GET", "/v1/admin/decisions", params=params | {"from": seen})["items"]
        for d in reversed(items):
            if args.json:
                say(json.dumps(d))
            else:
                probs = ", ".join(f"{x:.3f}" for x in d["probabilities"] or []) or "refused"
                name = d.get("question_name") or d["question_id"][:10]
                line = (
                    f"{d['ts']}  {name}  {d['model_served']}  [{probs}]  "
                    f"{d.get('action_taken') or '-'}  {d.get('external_ref') or ''}"
                )
                say(printable(line).rstrip())  # one decision, one line
        if items:
            seen = items[0]["ts"]
        if args.once:
            return
        sys.stdout.flush()
        time.sleep(args.interval)


def cmd_config(args: argparse.Namespace) -> None:
    path = config_path()
    if args.action == "show":
        saved = load_config()
        key = saved.get("key")
        say(f"url: {saved.get('url', '-')}")
        say(f"key: {key[:8] + '...' if key else '-'}")
        say(f"file: {path}")
        return
    key = args.key or getpass.getpass("trimset key: ")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": args.config_url.rstrip("/"), "key": key}, f)
    os.chmod(path, 0o600)
    say(f"saved to {path}")


def _q(question: str) -> str:
    return urllib.parse.quote(question, safe="")


# ------------------------------------------------------------------ main


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="trimset", description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    root.add_argument("--json", action="store_true", help="print JSON instead of tables")
    root.add_argument("--url", help="the trimset server (default: TRIMSET_URL or the config)")
    sub = root.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="save the server's URL and your key")
    config.add_argument("action", choices=["set", "show"])
    config.add_argument("--url", dest="config_url", help="the trimset server")
    config.add_argument(
        "--key", help="the key (asked for when left out, so it stays out of shell history)"
    )

    def group(p: argparse.ArgumentParser, model_required: bool = True) -> None:
        p.add_argument("question", help="the question's name or id")
        p.add_argument("--model", required=model_required, help="the served model, e.g. jev-1.14.0")

    calibrate = sub.add_parser("calibrate", help="fit calibration and show raw against calibrated")
    group(calibrate)
    calibrate.add_argument("--window-days", type=int, default=90)

    simulate = sub.add_parser(
        "simulate", help="what candidate thresholds would do on the eval split"
    )
    group(simulate)
    simulate.add_argument("--lo", type=float, help="noul and score: the lower threshold")
    simulate.add_argument("--hi", type=float, help="noul and score: the upper threshold")
    simulate.add_argument("--t", type=float, help="choice: the top option's threshold")
    simulate.add_argument("--fit", help="a fit id (default: the latest)")
    simulate.add_argument("--top", type=int, default=15, help="rows to show from a grid")
    simulate.add_argument("--cost-escalation", type=float)
    simulate.add_argument("--cost-wrong", action="append", metavar="ACTION=COST")

    policy = sub.add_parser("policy", help="compile, show and diff policies").add_subparsers(
        dest="policy_command", required=True
    )
    compile_ = policy.add_parser("compile", help="compile a draft policy from a target")
    group(compile_)
    compile_.add_argument(
        "--false-pass", type=float, help="the most wrong ACCEPTs allowed, e.g. 0.02"
    )
    compile_.add_argument(
        "--error-budget", type=float, help="the most wrong decisions on every rule"
    )
    compile_.add_argument("--confidence", type=float, default=0.95)
    compile_.add_argument("--max-escalation", type=float)
    compile_.add_argument(
        "--cost-escalation", type=float, help="a cost target: the cost of one escalation"
    )
    compile_.add_argument("--cost-wrong", action="append", metavar="ACTION=COST")
    compile_.add_argument("--audit-fraction", type=float)
    compile_.add_argument("--fit", help="a fit id (default: the latest)")
    show = policy.add_parser("show", help="a policy's rules, guarantee and metrics")
    show.add_argument("policy_id", nargs="?")
    show.add_argument("--question")
    show.add_argument("--model")
    diff = policy.add_parser("diff", help="a policy against the active one, or --against")
    diff.add_argument("policy_id")
    diff.add_argument("--against")

    coverage = sub.add_parser("coverage", help="labels per served model and action path")
    group(coverage, model_required=False)

    decisions = sub.add_parser("decisions", help="watch decisions arrive").add_subparsers(
        dest="decisions_command", required=True
    )
    tail = decisions.add_parser("tail", help="print decisions as they arrive")
    tail.add_argument("--question")
    tail.add_argument("--model")
    tail.add_argument("--interval", type=float, default=2.0)
    tail.add_argument("--once", action="store_true", help="print the latest and stop")
    return root


Command = Callable[[Client, argparse.Namespace], None]
COMMANDS: dict[str | tuple[str, str], Command] = {
    "calibrate": cmd_calibrate,
    "simulate": cmd_simulate,
    "coverage": cmd_coverage,
    ("policy", "compile"): cmd_compile,
    ("policy", "show"): cmd_show,
    ("policy", "diff"): cmd_diff,
    ("decisions", "tail"): cmd_tail,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "config":
            if args.action == "set" and not args.config_url:
                raise CliError("trimset config set needs --url")
            cmd_config(args)
            return 0
        settings = load_config()
        url = args.url or settings.get("url")
        if not url or not settings.get("key"):
            raise CliError("no server configured: run trimset config set --url URL")
        client = Client(url, settings["key"])
        sub = getattr(args, f"{args.command}_command", None)
        command = COMMANDS[(args.command, sub) if sub else args.command]
        command(client, args)
    except CliError as e:
        say(f"trimset: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 0
    return 0
