import json
import logging
import logging.config
import sys
import time

import pytest

from trimset_server.logs import JsonFormatter, log_config, redact

KEY = "tsp_" + "A1b2C3d4" * 4  # a proxy key: prefix + 32 base62 characters


@pytest.mark.req("NFR-11")
@pytest.mark.parametrize("scope", ["tsp_", "tsi_", "tsr_", "tsa_"])
def test_every_key_scope_is_redacted(scope: str) -> None:
    assert redact(f"key={scope}{'x' * 32} end") == f"key={scope}[redacted] end"


@pytest.mark.req("NFR-11")
def test_proxy_key_in_an_access_log_path_is_redacted() -> None:
    line = f'127.0.0.1:5000 - "POST /p/{KEY}/v1/systemone HTTP/1.1" 200'
    assert KEY not in redact(line)
    assert "/p/tsp_[redacted]/v1/systemone" in redact(line)


@pytest.mark.req("NFR-11")
@pytest.mark.parametrize(
    "line",
    [
        f"x_{KEY}",  # right after an underscore
        f"GET /p%2F{KEY}/v1/systemone",  # right after an encoded slash
        "GET /p/tsp_%41" + KEY[5:] + "/v1",  # one character of the key encoded
        "GET /p/%2574sp_" + KEY[4:] + "/v1",  # the prefix double-encoded
        "GET /p/%2525252574sp_" + KEY[4:] + "/v1",  # ... and five times over
    ],
)
def test_keys_hidden_by_neighbours_or_encoding_are_still_redacted(line: str) -> None:
    out = redact(line)
    assert KEY[4:] not in out
    assert "tsp_[redacted]" in out


@pytest.mark.req("NFR-11")
@pytest.mark.req("NFR-13")
def test_over_encoded_paths_are_hidden_but_the_request_is_still_logged() -> None:
    path = "/p/%" + "25" * 5000 + "74sp_" + KEY[4:] + "/v1"  # 5,000 levels deep
    line = f'203.0.113.7:51234 - "POST {path} HTTP/1.1" 401'
    started = time.perf_counter()
    out = redact(line)
    assert time.perf_counter() - started < 0.5
    assert KEY[4:] not in out
    assert out == '203.0.113.7:51234 - "POST [redacted: over-encoded] HTTP/1.1" 401'


def test_a_lone_percent_sign_does_not_loop() -> None:
    assert redact("50% done, 100%") == "50% done, 100%"


def test_ordinary_text_is_left_alone() -> None:
    text = "tsp_ alone, the word tsa_short, and /v1/systemone"
    assert redact(text) == text


def _record(msg: str, *args: object, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise RuntimeError(f"boom {KEY}")
        except RuntimeError:
            exc_info = sys.exc_info()
    return logging.LogRecord("trimset.test", logging.WARNING, __file__, 1, msg, args, exc_info)


@pytest.mark.req("NFR-13")
def test_json_lines_carry_level_logger_and_redacted_message() -> None:
    out = json.loads(JsonFormatter().format(_record("hello %s", KEY)))
    assert out["level"] == "WARNING"
    assert out["logger"] == "trimset.test"
    assert out["message"] == "hello tsp_[redacted]"
    assert out["ts"].endswith("+00:00")


@pytest.mark.req("NFR-11")
def test_tracebacks_are_redacted_too() -> None:
    out = json.loads(JsonFormatter().format(_record("failed", exc=True)))
    assert "RuntimeError" in out["exc"]
    assert KEY not in out["exc"]


def test_log_config_is_valid_for_dictconfig() -> None:
    logging.config.dictConfig(log_config("INFO"))
    handler = logging.getLogger("uvicorn.access").handlers[0]
    assert isinstance(handler.formatter, JsonFormatter)
