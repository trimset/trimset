import json
import logging
import logging.config
import sys

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
    ],
)
def test_keys_hidden_by_neighbours_or_encoding_are_still_redacted(line: str) -> None:
    out = redact(line)
    assert KEY[4:] not in out
    assert "tsp_[redacted]" in out


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
