import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from trimset_server.app import create_app
from trimset_server.config import Settings
from trimset_server.telemetry import setup_telemetry

UNREACHABLE = Settings(database_url="postgresql://u:p@127.0.0.1:1/trimset")
KEY = "tsp_" + "Zz9" * 10 + "Qq"  # 32 characters after the prefix


def test_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_telemetry(create_app(UNREACHABLE)) is False


@pytest.mark.req("NFR-12")
@pytest.mark.req("NFR-13")
def test_spans_never_carry_a_proxy_key() -> None:
    exporter = InMemorySpanExporter()
    app = create_app(UNREACHABLE)
    assert setup_telemetry(app, exporter) is True

    with TestClient(app) as client:
        client.post(f"/p/{KEY}/v1/systemone", json={})  # no such route yet: a 404 span

    spans = exporter.get_finished_spans()
    assert spans, "the request should have produced spans"
    for span in spans:
        assert KEY not in span.name
        for value in (span.attributes or {}).values():
            assert KEY not in str(value)
