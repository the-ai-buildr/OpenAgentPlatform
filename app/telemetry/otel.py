"""OpenTelemetry wiring.

`init_tracing()` must run *before* any SQLAlchemy engine, HTTPX client or Agno
object is constructed: the SQLAlchemy and HTTPX instrumentors patch the library
at class level, so engines and clients created earlier are never traced.
`app.main` therefore calls it at the very top of the module, before importing
anything that touches the database.
"""

from __future__ import annotations

import logging
from typing import Any

from app.settings import settings

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {"initialized": False, "error": None, "instrumentations": []}


def init_tracing() -> bool:
    """Configure the tracer provider and library instrumentation. Idempotent."""
    if _state["initialized"] or not settings.otel_enabled:
        return bool(_state["initialized"])

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.instance.id": settings.oap_id,
                "deployment.environment": settings.otel_environment,
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otel_traces_endpoint,
                    headers=settings.parsed_otel_headers() or None,
                )
            )
        )
        trace.set_tracer_provider(provider)
    except Exception as exc:  # pragma: no cover - depends on optional extras
        _state["error"] = f"tracer provider: {exc}"
        logger.warning("OpenTelemetry disabled: %s", exc)
        return False

    _instrument_libraries()
    _state["initialized"] = True
    logger.info("OpenTelemetry exporting to %s", settings.otel_traces_endpoint)
    return True


def _instrument_libraries() -> None:
    """Best-effort library instrumentation; a missing extra must not be fatal."""
    done: list[str] = _state["instrumentations"]

    def attempt(name: str, fn: Any) -> None:
        try:
            fn()
            done.append(name)
        except Exception as exc:  # pragma: no cover - optional dependencies
            logger.warning("OpenTelemetry instrumentation for %s failed: %s", name, exc)

    def _sqlalchemy() -> None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()

    def _httpx() -> None:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()

    def _agno() -> None:
        from openinference.instrumentation.agno import AgnoInstrumentor

        AgnoInstrumentor().instrument()

    attempt("sqlalchemy", _sqlalchemy)
    attempt("httpx", _httpx)
    attempt("agno", _agno)


def instrument_app(app: Any) -> None:
    """Attach FastAPI request tracing. Safe to call when tracing is disabled."""
    if not _state["initialized"]:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
        _state["instrumentations"].append("fastapi")
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("OpenTelemetry FastAPI instrumentation failed: %s", exc)


def get_tracer(name: str = "oap") -> Any:
    from opentelemetry import trace

    return trace.get_tracer(name)


def get_otel_status() -> dict[str, Any]:
    return {
        "enabled": settings.otel_enabled,
        "initialized": bool(_state["initialized"]),
        "service_name": settings.otel_service_name,
        "environment": settings.otel_environment,
        "otlp_endpoint": settings.otel_traces_endpoint,
        "instrumented": list(_state["instrumentations"]),
        "error": _state["error"],
    }
