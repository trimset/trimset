"""A stand-in decision model for tests and demos (FR-59). Never for production.

    trimset-mock-upstream --port 9000 --seed 7

- `POST /v1/systemone` answers in the systemone format, `POST /v1/decisions` in OpenAI's,
  and `GET /v1/models` lists one model (docs/03-api/wire-formats.md).
- Answers are deterministic: the same seed, state and question always get the same
  probabilities. The version reshapes them (sharper or flatter, leaning one way), the
  way a new model release does. A systemone request for `<name>-latest` is served as
  `<name>-<version>`, so switching the version is a silent upgrade; OpenAI's format
  echoes the requested name, as OpenAI does.
- `PATCH /_mock/config` switches the version, adds latency or injects errors while it
  runs. `GET /_mock/last` shows the last request's path and headers.
"""

import argparse
import asyncio
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


@dataclass(frozen=True)
class Config:
    seed: int = 0
    version: str = "1.0.0"
    latency_ms: float = 0.0
    error_rate: float = 0.0  # the share of requests answered with error_status
    error_status: int = 503

    def __post_init__(self) -> None:
        number = (int, float)
        types: dict[str, type | tuple[type, ...]] = {
            "seed": int,
            "version": str,
            "latency_ms": number,
            "error_rate": number,
            "error_status": int,
        }
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, types[f.name]):
                raise ValueError(f"{f.name} has the wrong type")
        if not 1 <= len(self.version) <= 64:
            raise ValueError("version must be 1 to 64 characters")
        if not 0 <= self.latency_ms <= 60_000:
            raise ValueError("latency_ms must be from 0 to 60000")
        if not 0 <= self.error_rate <= 1:
            raise ValueError("error_rate must be from 0 to 1")
        if not 400 <= self.error_status <= 599:
            raise ValueError("error_status must be from 400 to 599")


class BadRequest(ValueError):
    pass


def _rng(*parts: Any) -> random.Random:
    key = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return random.Random(hashlib.sha256(key.encode()).digest())  # noqa: S311 (test data)


def probabilities(config: Config, state: Any, question: Any, k: int) -> list[float]:
    """k probabilities (one, the probability of yes, when k is 1), rounded to 4 decimals
    like the vendors and summing to 1. The seed, state and question pick the answer; the
    version sharpens or flattens it and tilts it towards the first option."""
    base = _rng(config.seed, state, question)
    tilt = _rng(config.seed, config.version)
    sharpness, lean = tilt.uniform(0.6, 1.6), tilt.uniform(-1.0, 1.0)
    if k == 1:
        u = min(max(base.random(), 1e-6), 1 - 1e-6)
        z = sharpness * math.log(u / (1 - u)) + lean
        return [round(1 / (1 + math.exp(-z)), 4)]
    logits = [3 * sharpness * math.log(base.random() + 1e-9) for _ in range(k)]
    logits[0] += lean
    top_logit = max(logits)
    weights = [math.exp(x - top_logit) for x in logits]
    p = [round(w / sum(weights), 4) for w in weights]
    top = p.index(max(p))
    p[top] = round(max(0.0, 1 - (sum(p) - p[top])), 4)
    return p


def _systemone(config: Config, body: dict[str, Any]) -> dict[str, Any]:
    questions, state = body.get("questions"), body.get("state")
    if not isinstance(questions, dict) or not questions:
        raise BadRequest("`questions` must be a non-empty object")
    answers: dict[str, Any] = {}
    for name, q in questions.items():
        kind = q.get("type") if isinstance(q, dict) else None
        criteria = q.get("criteria") if isinstance(q, dict) else None
        if kind == "noul":
            answers[name] = {"type": "noul", "noul": probabilities(config, state, [name, q], 1)[0]}
        elif kind == "choice" and isinstance(criteria, dict) and criteria:
            keys = list(criteria)
            p = probabilities(config, state, [name, q], len(keys))
            top = p.index(max(p))
            answers[name] = {
                "type": "choice",
                "choice": keys[top],
                "confidence": p[top],
                "probabilities": dict(zip(keys, p, strict=True)),
            }
        elif kind == "score" and isinstance(criteria, list) and criteria:
            p = probabilities(config, state, [name, q], len(criteria))
            answers[name] = {
                "type": "score",
                "score": round(sum(i * x for i, x in enumerate(p)), 4),
                "confidence": max(p),
                "legend": {str(i): c for i, c in enumerate(criteria)},
                "probabilities": {str(i): x for i, x in enumerate(p)},
            }
        else:
            raise BadRequest(f"question {name!r}: a noul, choice or score with its criteria")
    model = body["model"]
    served = model.removesuffix("latest") + config.version if model.endswith("-latest") else model
    return {"model": served, "answers": answers, "usage": _usage(body, len(answers))}


def _openai(config: Config, body: dict[str, Any]) -> dict[str, Any]:
    questions, state = body.get("questions"), body.get("input")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 400:
        raise BadRequest("`questions` must be an array of 1 to 400")
    answers: list[dict[str, Any]] = []
    for i, q in enumerate(questions):
        kind = q.get("type") if isinstance(q, dict) else None
        options = (q.get("choices") or q.get("levels")) if isinstance(q, dict) else None
        name = q.get("name") if isinstance(q, dict) else None
        if kind == "predicate":
            p = probabilities(config, state, q, 1)
            answers.append({"type": "predicate", "name": name, "probability": p[0]})
            continue
        if kind not in ("choice", "score") or not isinstance(options, list) or not options:
            raise BadRequest(f"questions[{i}]: a predicate, choice or score with its options")
        if not all(isinstance(o, dict) for o in options):
            raise BadRequest(f"questions[{i}]: each option is an object")
        p = probabilities(config, state, q, len(options))
        top = p.index(max(p))
        if kind == "choice":
            answers.append(
                {
                    "type": "choice",
                    "name": name,
                    "choice": options[top].get("value"),
                    "confidence": p[top],
                    "probabilities": [
                        {"value": o.get("value"), "probability": x}
                        for o, x in zip(options, p, strict=True)
                    ],
                }
            )
        else:
            answers.append(
                {
                    "type": "score",
                    "name": name,
                    "score": round(sum(j * x for j, x in enumerate(p)), 4),
                    "confidence": p[top],
                    "probabilities": [
                        {"label": o.get("label"), "probability": x, "value": j}
                        for j, (o, x) in enumerate(zip(options, p, strict=True))
                    ],
                }
            )
    usage = _usage(body, len(answers))
    usage |= {"total_tokens": usage["input_tokens"] + usage["output_tokens"]}
    usage |= {"input_tokens_details": {"cached_tokens": 0}}
    return {"model": body["model"], "answers": answers, "usage": usage}


def _usage(body: dict[str, Any], answers: int) -> dict[str, Any]:
    return {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": answers}


def _error(fmt: str, status: int, message: str) -> JSONResponse:
    if fmt == "systemone":
        content: dict[str, Any] = {"detail": message}
    else:
        content = {"error": {"message": message, "type": "invalid_request_error", "code": None}}
        if status >= 500 or status == 429:
            content["error"]["type"] = "server_error"
    headers = {"retry-after": "1"} if status == 429 else None
    return JSONResponse(content, status_code=status, headers=headers)


async def _answer(request: Request, fmt: str) -> JSONResponse:
    app = request.app.state
    app.last = {
        "method": request.method,
        "path": request.url.path,
        "headers": dict(request.headers),
    }
    app.count += 1
    config: Config = app.config
    if config.latency_ms:
        await asyncio.sleep(config.latency_ms / 1000)
    if _rng(config.seed, "errors", app.count).random() < config.error_rate:
        return _error(fmt, config.error_status, "the mock upstream failed on purpose")
    if request.method == "GET":
        return JSONResponse({"object": "list", "data": [{"id": "mock-latest", "object": "model"}]})

    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return _error(fmt, 415, "the mock upstream takes JSON only")
    try:
        body = json.loads(await request.body(), parse_constant=_no_constants)
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise BadRequest("the body must be an object with a `model`")
        answer = _systemone(config, body) if fmt == "systemone" else _openai(config, body)
    except (ValueError, BadRequest) as e:
        return _error(fmt, 400, str(e) if isinstance(e, BadRequest) else "the body is not JSON")
    return JSONResponse(answer)


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


async def systemone(request: Request) -> JSONResponse:
    return await _answer(request, "systemone")


async def decisions(request: Request) -> JSONResponse:
    return await _answer(request, "openai-decisions")


async def config_route(request: Request) -> JSONResponse:
    if request.method == "PATCH":
        try:
            patch = json.loads(await request.body())
            if not isinstance(patch, dict) or not patch.keys() <= set(
                asdict(request.app.state.config)
            ):
                raise ValueError("a merge patch of the config's own fields")
            request.app.state.config = replace(request.app.state.config, **patch)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
    return JSONResponse(asdict(request.app.state.config))


async def last(request: Request) -> JSONResponse:
    state = request.app.state
    return JSONResponse({"count": state.count, "last": state.last})


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(config: Config | None = None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/v1/systemone", systemone, methods=["POST"]),
            Route("/v1/decisions", decisions, methods=["POST"]),
            Route("/v1/models", decisions, methods=["GET"]),
            Route("/_mock/config", config_route, methods=["GET", "PATCH"]),
            Route("/_mock/last", last, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )
    app.state.config = config or Config()
    app.state.count = 0
    app.state.last = None
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="trimset-mock-upstream", description="A stand-in decision model (FR-59)."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--error-status", type=int, default=503)
    args = parser.parse_args(argv)
    try:
        config = Config(
            args.seed, args.version, args.latency_ms, args.error_rate, args.error_status
        )
    except ValueError as e:
        parser.error(str(e))
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")
