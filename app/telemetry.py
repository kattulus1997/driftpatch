from __future__ import annotations

import os

import google.auth
from google.auth.transport.requests import AuthorizedSession
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_TELEMETRY_ENDPOINT = "https://telemetry.googleapis.com/v1/traces"
_provider: TracerProvider | None = None


def _disable_model_content_capture() -> None:
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "false"
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "NO_CONTENT"


def configure_cloud_tracing() -> None:
    global _provider

    if os.getenv("CLOUD_TELEMETRY_ENABLED", "false").lower() != "true":
        return
    if _provider is not None:
        return

    _disable_model_content_capture()

    credentials, project_id = google.auth.default()
    if not project_id:
        raise RuntimeError("Google Cloud tracing requires a project ID")

    exporter = OTLPSpanExporter(
        endpoint=_TELEMETRY_ENDPOINT,
        session=AuthorizedSession(credentials),
    )
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "driftpatch-worker",
                "gcp.project_id": project_id,
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _provider = provider


def shutdown_cloud_tracing() -> None:
    if _provider is not None:
        _provider.shutdown()
