"""The support app of the M3 demo: it routes tickets with a decision model, and lets
trimset's SDK decide what to do with each answer, locally, from the live policy.

Run by run.sh inside the server container, where the SDK is installed. Reads
TRIMSET_INGEST_KEY from the environment. Two parts:

  1. Route six tickets: gate() each answer, log the exchange with the action taken, and
     report what the customer's ticket turned out to need.
  2. Watch the policy: ask about the same ticket every 0.2 s, and print each time the
     policy behind the answer changes (an activation, a rollback). Leaves once it has
     seen version 1, then 2, then 1 again, or after WATCH_SECONDS.
"""

import os
import sys
import time

import trimset

URL = "http://127.0.0.1:8810"
MODEL = "jev-1.14.0"
WATCH_SECONDS = float(os.environ.get("WATCH_SECONDS", "90"))

# What the model said about six tickets: its probability for billing, shipping, returns, tech.
TICKETS = [
    ("t-1", "Where is my parcel? It was due on Monday.", [0.02, 0.93, 0.03, 0.02], "shipping"),
    ("t-2", "I was charged twice for the same order.", [0.90, 0.03, 0.04, 0.03], "billing"),
    ("t-3", "The app crashes when I open my basket.", [0.03, 0.02, 0.05, 0.90], "tech"),
    ("t-4", "These shoes are too small; how do I send them back?", [0.05, 0.10, 0.80, 0.05],
     "returns"),
    ("t-5", "Something is wrong with my order.", [0.30, 0.30, 0.25, 0.15], "shipping"),
    ("t-6", "Refund me or I'm calling my bank.", [0.45, 0.10, 0.40, 0.05], "billing"),
]  # fmt: skip


def say(text: str = "") -> None:
    print(text, flush=True)


def exchange(options: list[dict], definition: dict, ticket: str, probs: list[float]) -> tuple:
    """The call the app made to the decision model, and what came back (systemone)."""
    request = {
        "model": "jev-latest",
        "state": {"ticket": ticket},
        "questions": {
            "team": {
                "type": "choice",
                "instructions": definition["instructions"],
                "criteria": {o["value"]: o["description"] for o in options},
            }
        },
    }
    answer = {
        "type": "choice",
        "probabilities": {o["value"]: p for o, p in zip(options, probs, strict=True)},
    }
    return request, {"model": MODEL, "answers": {"team": answer}}, answer


def main() -> int:
    ts = trimset.Client(
        URL, os.environ["TRIMSET_INGEST_KEY"], defaults={"team": "ESCALATE"},
        poll_interval=1.0, flush_interval=0.2,
    )  # fmt: skip
    if not ts.wait_for_policies(15):
        say("no policies after 15 s")
        return 1
    definition = ts.snapshot.questions["team"]["definition"]
    options = definition["options"]

    say("route six tickets (the app asks the model, trimset's policy decides):")
    say(f"{'ticket':8} {'model says':22} {'action':16} {'by':22}")
    for ref, text, probs, truth in TICKETS:
        request, response, answer = exchange(options, definition, text, probs)
        d = ts.gate("team", answer=answer, model_served=MODEL)
        top = options[probs.index(max(probs))]["value"]
        by = f"policy v{d.policy_version}" if d.policy_version else f"default ({d.reason})"
        say(f"{ref:8} {f'{top} {max(probs):.0%}':22} {d.action:16} {by:22}")
        ts.log_exchange(request, response, format="systemone", gates={"team": d}, external_ref=ref)
        ts.outcome(external_ref=ref, question="team", label=truth, source="downstream")
    ts.flush(10)
    say()

    # The ticket to watch: a borderline one, so that a different policy may treat it differently.
    probs = TICKETS[5][2]
    _, _, answer = exchange(options, definition, "watch", probs)
    say(f"watching the policy for ticket t-6 ({TICKETS[5][1]!r}) ...")
    seen: list[int] = []
    last = None
    start = time.monotonic()
    while time.monotonic() - start < WATCH_SECONDS:
        d = ts.gate("team", answer=answer, model_served=MODEL)
        if d.policy_version != last:
            was = f"v{last}" if last else "nothing"
            at = time.monotonic() - start
            say(f"  +{at:5.1f} s  policy v{d.policy_version} (was {was}): t-6 gets {d.action}")
            last = d.policy_version
            if last is not None:
                seen.append(last)
        if seen[:3] == [1, 2, 1]:
            say("the whole round trip was seen: v1, then v2, then v1 again")
            ts.close()
            return 0
        time.sleep(0.2)
    say(f"gave up after {WATCH_SECONDS:.0f} s; saw versions {seen}")
    ts.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
