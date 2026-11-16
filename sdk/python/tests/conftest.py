"""Shared fakes for the SDK's tests: a trimset server that serves the policy bundle,
answers the proxy call, and records what the SDK reports."""

import copy
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from trimset import agent
from trimset.agent import question_definition
from trimset.hashing import fingerprint, question_id

EXAMPLES = Path(__file__).parents[3] / "docs" / "03-api" / "examples"
EXAMPLE = json.loads((EXAMPLES / "bundle.json").read_text())
MODEL = "jev-1.14.0"
DEFINITION = question_definition()
QID = question_id(DEFINITION)


def bundle_of(*, policy: bool = True, instructions: str | None = None) -> dict[str, Any]:
    """A bundle with the agent question, and an approved policy for MODEL: ACCEPT from
    0.85 up, BLOCK below 0.30, a person in between."""
    definition = copy.deepcopy(DEFINITION)
    if instructions is not None:
        definition["instructions"] = instructions
    qid = question_id(definition)
    body = copy.deepcopy(EXAMPLE["policies"][0]["policy"]) | {
        "question_id": qid,
        "question_name": agent.QUESTION,
        "type": "noul",
        "version": 7,
        "model_served": MODEL,
        "calibration": {"method": "identity"},
        "audit_fraction": 0,
        "rules": [
            {"id": "accept", "action": "ACCEPT", "scalar": "p", "op": "ge", "threshold": 0.85},
            {"id": "block", "action": "BLOCK", "scalar": "p", "op": "lt", "threshold": 0.30},
        ],
    }
    body["fingerprint"] = fingerprint(body)
    return EXAMPLE | {
        "questions": {agent.QUESTION: {"question_id": qid, "definition": definition}},
        "policies": [{"policy": body, "guarantee_status": "certified"}] if policy else [],
    }


class Trimset:
    """The parts of trimset an agent gate talks to."""

    def __init__(self, bundle: dict[str, Any], p: float | Callable[[Any], float] = 0.97) -> None:
        self.bundle, self.p, self.served = bundle, p, MODEL
        self.status = 200
        self.asked: list[dict[str, Any]] = []
        self.posts: list[tuple[str, Any]] = []
        self.redirect = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._send(200, server.bundle, {"ETag": f'"{server.bundle["etag"]}"'})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/systemone":
                    server.asked.append(body | {"_key": self.headers.get("X-Trimset-Key")})
                    if server.redirect:
                        self.send_response(302)
                        self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if server.status != 200:
                        return self._send(server.status, {"title": "no"})
                    name = next(iter(body["questions"]))
                    p = server.p(body["state"]) if callable(server.p) else server.p
                    answer = {"type": "noul", "noul": p}
                    return self._send(
                        200,
                        {"model": server.served, "answers": {name: answer}},
                        {"X-Trimset-Decision-Id": str(uuid.uuid4())},
                    )
                server.posts.append((self.path, body))
                self._send(200, {"stored": 1, "duplicates": 0, "rejected": 0, "matched": 1})

            def log_message(self, *args: Any) -> None:
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def bundle_of(self, **options: Any) -> dict[str, Any]:
        return bundle_of(**options)

    def reports(self) -> list[dict[str, Any]]:
        return [
            r for path, b in self.posts if path == "/v1/decisions:actions" for r in b["actions"]
        ]

    def outcomes(self) -> list[dict[str, Any]]:
        return [o for path, b in self.posts if path == "/v1/outcomes" for o in b["outcomes"]]


@pytest.fixture
def trimset() -> Iterator[Trimset]:
    fake = Trimset(bundle_of())
    yield fake
    fake.http.shutdown()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read, or write, the developer's own config or cache."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("TRIMSET_URL", "TRIMSET_KEY", "TRIMSET_AGENT_MODEL"):
        monkeypatch.delenv(name, raising=False)
