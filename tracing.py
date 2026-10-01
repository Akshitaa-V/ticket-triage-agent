"""
OpenTelemetry tracing, exported to Langfuse.

Langfuse accepts standard OTLP traces at <LANGFUSE_HOST>/api/public/otel,
so nothing here is Langfuse-specific except the endpoint and the auth
header. Any other OTLP backend (Jaeger, Grafana Tempo, ...) would work by
changing the exporter.

If LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set, spans are still
created (so the code path is identical) but nothing is exported. Tests use
an in-memory exporter instead, see configure_tracing(exporter=...).
"""

import base64
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

SERVICE_NAME = "ticket-triage-agent"
_provider: TracerProvider | None = None


def _langfuse_exporter() -> SpanExporter | None:
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        return None

    # Imported lazily so the service still runs without the exporter package.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {auth}",
            "x-langfuse-ingestion-version": "4",
        },
    )


def configure_tracing(exporter: SpanExporter | None = None) -> TracerProvider:
    """
    Set up a tracer provider once. Pass `exporter` to override the Langfuse
    exporter (tests pass an InMemorySpanExporter).
    """
    global _provider
    if _provider is None:
        _provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        trace.set_tracer_provider(_provider)
        langfuse = _langfuse_exporter()
        if langfuse is not None:
            # Batch export so tracing never blocks a request.
            _provider.add_span_processor(BatchSpanProcessor(langfuse))
    if exporter is not None:
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _provider


def get_tracer() -> trace.Tracer:
    configure_tracing()
    return trace.get_tracer(SERVICE_NAME)


def shutdown_tracing() -> None:
    """Flush pending spans (called on app shutdown)."""
    if _provider is not None:
        _provider.shutdown()
