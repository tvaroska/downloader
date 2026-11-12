"""OpenTelemetry distributed tracing setup and context propagation helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from . import __version__
from .config import TelemetryConfig

logger = logging.getLogger(__name__)

_provider: TracerProvider | None = None
_instrumented_app: Any = None
_global_instrumented = False


def get_tracer(name: str = "downloader") -> trace.Tracer:
    """Return a tracer backed by the active TracerProvider (or no-op when disabled)."""
    return trace.get_tracer(name, __version__)


def get_current_trace_context() -> tuple[str | None, str | None]:
    """Return (trace_id_hex, span_id_hex) for the active valid span, or (None, None)."""
    span = trace.get_current_span()
    if span is None:
        return None, None
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def inject_trace_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Inject W3C traceparent/tracestate headers from the active context into a dict."""
    carrier: dict[str, str] = dict(headers) if headers else {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(headers: Mapping[str, str] | None) -> Context:
    """Extract an OpenTelemetry Context from W3C carrier headers."""
    if not headers:
        return Context()
    return propagate.extract(dict(headers))


def configure_telemetry(
    config: TelemetryConfig,
    *,
    app: Any = None,
    service_version: str = __version__,
    extra_exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Configure OpenTelemetry tracing and auto-instrumentation when enabled.

    Returns the configured TracerProvider or None when OTEL_ENABLED is false.
    """
    global _provider, _instrumented_app, _global_instrumented

    if not config.enabled:
        logger.debug("OpenTelemetry disabled (OTEL_ENABLED=false)")
        return None

    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.version": service_version,
        }
    )
    sampler = TraceIdRatioBased(config.traces_sample_rate)
    provider = TracerProvider(resource=resource, sampler=sampler)

    if config.exporter_otlp_endpoint:
        otlp_exporter = OTLPSpanExporter(endpoint=config.exporter_otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info(f"OpenTelemetry OTLP HTTP exporter configured: {config.exporter_otlp_endpoint}")

    if config.console_exporter:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))

    trace.set_tracer_provider(provider)
    _provider = provider

    if app is not None and _instrumented_app is not app:
        try:
            FastAPIInstrumentor.instrument_app(
                app,
                tracer_provider=provider,
                excluded_urls=r"^https?://[^/]+/(health|metrics)(\?.*)?$",
            )
            # Rebuild Starlette's compiled middleware stack when instrumented inside lifespan.
            if hasattr(app, "middleware_stack"):
                app.middleware_stack = None
            _instrumented_app = app
        except Exception as exc:
            logger.warning(f"Failed to instrument FastAPI app with OpenTelemetry: {exc}")

    if not _global_instrumented:
        try:
            HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        except Exception as exc:
            logger.debug(f"HTTPX OpenTelemetry instrumentation skipped: {exc}")
        try:
            RedisInstrumentor().instrument(tracer_provider=provider)
        except Exception as exc:
            logger.debug(f"Redis OpenTelemetry instrumentation skipped: {exc}")
        _global_instrumented = True

    logger.info(
        f"OpenTelemetry tracing initialized (service={config.service_name}, "
        f"sample_rate={config.traces_sample_rate})"
    )
    return provider


def shutdown_telemetry() -> None:
    """Flush and shut down the active OpenTelemetry TracerProvider."""
    global _provider, _instrumented_app, _global_instrumented

    if _instrumented_app is not None:
        try:
            FastAPIInstrumentor.uninstrument_app(_instrumented_app)
            if hasattr(_instrumented_app, "middleware_stack"):
                _instrumented_app.middleware_stack = None
        except Exception:
            pass
        _instrumented_app = None

    if _global_instrumented:
        for instr_cls in (HTTPXClientInstrumentor, RedisInstrumentor):
            try:
                instr_cls().uninstrument()
            except Exception:
                pass
        _global_instrumented = False

    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as exc:
            logger.warning(f"Error shutting down OpenTelemetry provider: {exc}")
        _provider = None
