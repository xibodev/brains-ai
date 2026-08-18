"""OpenTelemetry wiring for the Brains gateway.

The whole module is a no-op when ``subsystems.otel.enabled`` is False so
the lean install never pays the cost of importing the OTel SDK. When the
operator opts in, we:

* fail loud if ``brains-ai[otel]`` is not installed (the extras gate),
* build a TracerProvider with a Resource carrying ``service.name``,
* attach a BatchSpanProcessor + OTLPSpanExporter (HTTP),
* instrument the FastAPI app so request spans are recorded,
* set the provider as the global tracer provider.

Endpoint and service name resolution follows the OTel convention: the
``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_SERVICE_NAME`` env vars win over
anything in the overlay, and missing values fall back to the SDK default
so a local collector on :4318 just works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from brains.extras import require_extra
from brains.observability.metrics import (
    PASSTHROUGH_INTEGRITY,
    bounded_label,
    counter_value,
    init_counter,
    record_passthrough_mutation,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


@dataclass(frozen=True)
class OtelStatus:
    """Snapshot of the OTel subsystem for health endpoints."""

    enabled: bool
    configured: bool
    endpoint: str | None
    service_name: str
    detail: str = ""


def _resolve_endpoint(settings: Any) -> str | None:
    return (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or settings.subsystems.otel.endpoint or None
    )


def _resolve_service_name(settings: Any) -> str:
    return os.environ.get("OTEL_SERVICE_NAME") or settings.subsystems.otel.service_name


def status(settings: Any) -> OtelStatus:
    """Describe the OTel subsystem without importing the SDK."""
    enabled = bool(settings.subsystems.otel.enabled)
    endpoint = _resolve_endpoint(settings)
    service_name = _resolve_service_name(settings)
    configured = bool(enabled and endpoint)
    if not enabled:
        detail = "subsystems.otel.enabled=false"
    elif not endpoint:
        detail = (
            "OTEL_EXPORTER_OTLP_ENDPOINT not set and no endpoint in overlay;"
            " exporter will use the OTel SDK default (http://localhost:4318)."
        )
    else:
        detail = "ready"
    return OtelStatus(
        enabled=enabled,
        configured=configured,
        endpoint=endpoint,
        service_name=service_name,
        detail=detail,
    )


def _load_otel_modules() -> dict[str, Any]:
    """Deferred OTel import.

    Kept behind a function so tests can monkeypatch the resolution path
    without having to install the full OTel stack into sys.modules.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return {
        "trace": otel_trace,
        "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "Resource": Resource,
        "OTLPSpanExporter": OTLPSpanExporter,
        "FastAPIInstrumentor": FastAPIInstrumentor,
    }


def configure_otel(app: FastAPI, settings: Any) -> OtelStatus:
    """Wire OpenTelemetry tracing into the FastAPI app.

    Returns the resulting :class:`OtelStatus`. When the subsystem is
    disabled this is a fast no-op and no OTel modules are imported.
    """
    snapshot = status(settings)
    if not snapshot.enabled:
        return snapshot

    require_extra("otel", "subsystems.otel")
    modules = _load_otel_modules()

    resource = modules["Resource"].create({"service.name": snapshot.service_name})
    provider = modules["TracerProvider"](resource=resource)

    exporter_kwargs: dict[str, Any] = {}
    if snapshot.endpoint:
        exporter_kwargs["endpoint"] = snapshot.endpoint
    exporter = modules["OTLPSpanExporter"](**exporter_kwargs)

    processor = modules["BatchSpanProcessor"](exporter)
    provider.add_span_processor(processor)

    modules["trace"].set_tracer_provider(provider)
    modules["FastAPIInstrumentor"].instrument_app(app)
    return snapshot


__all__ = [
    "OtelStatus",
    "PASSTHROUGH_INTEGRITY",
    "bounded_label",
    "configure_otel",
    "counter_value",
    "init_counter",
    "record_passthrough_mutation",
    "status",
]
