"""Errors as RFC 9457 problem details (docs/03-api/README.md, "Errors")."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PROBLEM_BASE = "https://trimset.dev/problems/"


class Problem(Exception):
    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(title)
        self.status, self.slug, self.title = status, slug, title
        self.detail, self.headers, self.errors = detail, headers, errors

    def response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "type": PROBLEM_BASE + self.slug,
            "title": self.title,
            "status": self.status,
        }
        if self.detail:
            body["detail"] = self.detail
        if self.errors:
            body["errors"] = self.errors
        return JSONResponse(
            body,
            status_code=self.status,
            headers=self.headers,
            media_type="application/problem+json",
        )


def install(app: FastAPI) -> None:
    async def on_problem(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, Problem):
            return exc.response()
        raise exc

    async def on_invalid(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):
            raise exc
        # Field paths and messages only; never echo the submitted values back.
        errors = [
            {"pointer": "/" + "/".join(str(p) for p in e.get("loc", ())), "message": e["msg"]}
            for e in exc.errors()
        ]
        return Problem(422, "invalid-request", "The request is not valid", errors=errors).response()

    app.add_exception_handler(Problem, on_problem)
    app.add_exception_handler(RequestValidationError, on_invalid)
