# syntax=docker/dockerfile:1
# One image for both processes (ADR 0004): `trimset-server serve`, and the worker from T1.4.
# Base images are pinned by digest; bump them on purpose, like dependencies.

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Third-party dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
COPY sdk/python/pyproject.toml sdk/python/README.md sdk/python/LICENSE sdk/python/
COPY stats/pyproject.toml stats/README.md stats/LICENSE stats/
COPY server/pyproject.toml server/README.md server/LICENSE server/
COPY tools/pyproject.toml tools/
RUN uv sync --locked --no-dev --no-editable --no-install-workspace --package trimset-server

COPY sdk/python/src sdk/python/src
COPY stats/src stats/src
COPY server/src server/src
RUN uv sync --locked --no-dev --no-editable --package trimset-server

# The mock upstream (FR-59), for tests and demos only: `docker build --target mock-upstream`,
# or compose.mock.yaml. The default target below never contains it.
FROM build AS mock-build
COPY tools/src tools/src
RUN UV_PROJECT_ENVIRONMENT=/mock/.venv uv sync --locked --no-dev --no-editable --package trimset-tools

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS mock-upstream
RUN useradd --system --uid 10001 --no-create-home trimset
COPY --from=mock-build /mock/.venv /mock/.venv
ENV PATH=/mock/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER 10001
EXPOSE 9000
ENTRYPOINT ["trimset-mock-upstream", "--host", "0.0.0.0"]


FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN useradd --system --uid 10001 --no-create-home trimset
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8800
ENTRYPOINT ["trimset-server"]
CMD ["serve"]
