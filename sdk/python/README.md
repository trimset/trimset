# trimset (Python SDK and CLI)

**Keep your decision thresholds true.** trimset is open-source, self-hosted decision ops
for decision models such as TypeSafe's Jev, Ollaya, Ollama and OpenAI's `/v1/decisions`.
It logs every decision, joins the real outcome when it arrives, compiles thresholds with
a stated guarantee, and lets your app apply the approved policy locally.

This package is the part that runs in your app. It needs a trimset server (one
`docker compose up`; see the [quickstart](https://github.com/trimset/trimset#quickstart)).

> **Alpha.** The interface may still change before 0.1.0.

```bash
pip install trimset
```

Pure Python, CPython 3.11 or newer, one dependency (`rfc8785`).

## Gate an answer, locally

```python
import trimset

ts = trimset.Client(
    "http://localhost:8800",
    key="tsi_...",                      # an ingest key
    defaults={"refund_fraud": "ESCALATE"},
)

d = ts.gate("refund_fraud", p=0.83, model_served="jev-1.14.0")
if d.action == "ACCEPT":
    ...
elif d.action == "BLOCK":
    ...
else:                                   # ESCALATE: a person decides
    ...
```

`gate()` applies the active, approved policy from a cache the client keeps fresh in the
background. It makes no network call, never raises and never blocks. When it has no
usable policy (none yet, another model version, a changed question), it returns your
default and says why in `d.reason`.

## Report back

```python
ts.log_exchange(request, response, format="systemone", gates={"refund_fraud": d},
                external_ref="rf-1001")
ts.outcome(external_ref="rf-1001", question="refund_fraud", label=True,
           source="downstream")      # a chargeback, 90 days later
```

Records and outcomes are queued and sent from a background thread, with bounded memory
and retries. The state of a request is hashed in your process and never sent.

A model that runs inside your own process, such as a Hugging Face pipeline, is captured
with `ts.instrument(model_callable, model="<repo>@<revision>")`.

## The CLI

```bash
trimset config set --url http://localhost:8800     # asks for an admin key
trimset calibrate refund_fraud --model jev-1.14.0
trimset policy compile refund_fraud --model jev-1.14.0 --false-pass 0.02
trimset policy approve POLICY_ID; trimset policy activate POLICY_ID
trimset policy rollback refund_fraud --model jev-1.14.0 --reason "..."
```

## More

- [Project and server](https://github.com/trimset/trimset), [trimset.dev](https://trimset.dev)
- [SDK interface](https://github.com/trimset/trimset/blob/main/docs/03-api/sdk.md): every
  method, the `gate()` algorithm and its results
- [Changelog](https://github.com/trimset/trimset/blob/main/CHANGELOG.md)

Apache-2.0.
