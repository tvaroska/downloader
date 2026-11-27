"""Unit tests for opt-in OpenTelemetry tracing and log trace correlation."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from downloader.config import TelemetryConfig, reload_settings
from downloader.logging_config import CustomJsonFormatter
from downloader.main import app
from downloader.telemetry import (
    configure_telemetry,
    extract_trace_context,
    get_tracer,
    inject_trace_headers,
    shutdown_telemetry,
)


class TestOpenTelemetryIntegration:
    def test_disabled_by_default_returns_none(self) -> None:
        assert configure_telemetry(TelemetryConfig(enabled=False)) is None

    def test_enabled_records_spans_correlates_logs_and_propagates_headers(self) -> None:
        exporter = InMemorySpanExporter()
        cfg = TelemetryConfig(enabled=True, service_name="downloader-test", traces_sample_rate=1.0)
        provider = configure_telemetry(cfg, app=app, extra_exporter=exporter)
        assert provider is not None
        try:
            tracer = get_tracer("test")
            formatter = CustomJsonFormatter()
            with tracer.start_as_current_span("parent.op") as span:
                # JSON log correlation
                record = logging.LogRecord(
                    name="downloader.test",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=1,
                    msg="hello traced log",
                    args=(),
                    exc_info=None,
                )
                formatted = json.loads(formatter.format(record))
                expected_trace = format(span.get_span_context().trace_id, "032x")
                assert formatted["trace_id"] == expected_trace
                assert "span_id" in formatted

                # W3C header propagation round-trip across Redis boundary
                carrier = inject_trace_headers()
                assert "traceparent" in carrier
                extracted_ctx = extract_trace_context(carrier)
                with tracer.start_as_current_span("child.worker", context=extracted_ctx) as child:
                    child_trace = format(child.get_span_context().trace_id, "032x")
                    assert child_trace == expected_trace

            spans = exporter.get_finished_spans()
            names = {s.name for s in spans}
            assert {"parent.op", "child.worker"}.issubset(names)
        finally:
            shutdown_telemetry()

    def test_lifespan_otel_rebuilds_middleware_and_emits_x_trace_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        reload_settings()
        try:
            with TestClient(app) as client:
                health = client.get("/health")
                assert health.status_code == 200
                assert "x-trace-id" not in health.headers

                blocked = client.get("/http://127.0.0.1:8000/health")
                assert blocked.status_code == 400
                trace_id = blocked.headers.get("x-trace-id")
                assert trace_id is not None and len(trace_id) == 32
        finally:
            monkeypatch.undo()
            reload_settings()

    def test_otlp_endpoint_appends_v1_traces_path(self) -> None:
        """Base OTLP collector URL gets `/v1/traces` appended while full path is preserved (`R111`)."""
        from downloader.telemetry import _resolve_otlp_traces_endpoint

        assert (
            _resolve_otlp_traces_endpoint("http://otel-collector:4318")
            == "http://otel-collector:4318/v1/traces"
        )
        assert (
            _resolve_otlp_traces_endpoint("http://otel-collector:4318/")
            == "http://otel-collector:4318/v1/traces"
        )
        assert (
            _resolve_otlp_traces_endpoint("http://otel-collector:4318/v1/traces")
            == "http://otel-collector:4318/v1/traces"
        )
