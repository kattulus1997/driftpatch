from __future__ import annotations

import os

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app import telemetry


def test_cloud_tracing_is_inert_without_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("CLOUD_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setattr(telemetry, "_provider", None)

    def unexpected_credentials():
        raise AssertionError("disabled tracing must not request credentials")

    monkeypatch.setattr(telemetry.google.auth, "default", unexpected_credentials)

    telemetry.configure_cloud_tracing()

    assert telemetry._provider is None


def test_cloud_tracing_uses_adc_and_the_google_otlp_endpoint(
    monkeypatch,
) -> None:
    exporter_args = {}
    providers = []
    credentials = object()
    session = object()
    exporter = InMemorySpanExporter()

    monkeypatch.setenv("CLOUD_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "SPAN_AND_EVENT",
    )
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(
        telemetry.google.auth,
        "default",
        lambda: (credentials, "driftpatch-test-project"),
    )
    monkeypatch.setattr(
        telemetry,
        "AuthorizedSession",
        lambda supplied: session if supplied is credentials else None,
    )

    def make_exporter(**kwargs):
        exporter_args.update(kwargs)
        return exporter

    monkeypatch.setattr(telemetry, "OTLPSpanExporter", make_exporter)
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", providers.append)

    telemetry.configure_cloud_tracing()

    assert exporter_args == {
        "endpoint": "https://telemetry.googleapis.com/v1/traces",
        "session": session,
    }
    assert providers == [telemetry._provider]
    assert telemetry._provider is not None
    assert telemetry._provider.resource.attributes["service.name"] == (
        "driftpatch-worker"
    )
    assert telemetry._provider.resource.attributes["gcp.project_id"] == (
        "driftpatch-test-project"
    )
    assert os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"
    assert (
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]
        == "NO_CONTENT"
    )
    telemetry.shutdown_cloud_tracing()
