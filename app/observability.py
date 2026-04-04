from __future__ import annotations

import json
import logging
import logging.config
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

logger = logging.getLogger(__name__)

_DEFAULT_PLAINTEXT_FORMAT = "%(levelname)s %(name)s: %(message)s"
_TRACING_INITIALIZED = False


@dataclass(frozen=True)
class LoggingSettings:
    level: str
    output_format: str
    plaintext_format: str
    json_message_key: str
    json_level_key: str
    json_logger_key: str
    json_timestamp_key: str
    json_exception_key: str
    json_static_fields: dict[str, Any]
    json_include_trace_context: bool


@dataclass(frozen=True)
class TracingSettings:
    enabled: bool
    service_name: str
    exporter: str
    otlp_endpoint: str | None
    otlp_headers: dict[str, str]
    otlp_timeout_seconds: float


class JsonLogFormatter(logging.Formatter):
    def __init__(
        self,
        *,
        message_key: str,
        level_key: str,
        logger_key: str,
        timestamp_key: str,
        exception_key: str,
        static_fields: dict[str, Any],
        include_trace_context: bool,
    ) -> None:
        super().__init__()
        self._message_key = message_key
        self._level_key = level_key
        self._logger_key = logger_key
        self._timestamp_key = timestamp_key
        self._exception_key = exception_key
        self._static_fields = static_fields
        self._include_trace_context = include_trace_context

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            self._timestamp_key: datetime.now(timezone.utc).isoformat(),
            self._level_key: record.levelname,
            self._logger_key: record.name,
            self._message_key: record.getMessage(),
            **self._static_fields,
        }
        if record.exc_info:
            payload[self._exception_key] = self.formatException(record.exc_info)
        if self._include_trace_context:
            payload.update(_current_trace_context())
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging() -> None:
    settings = load_logging_settings_from_env()
    formatter: dict[str, Any]
    if settings.output_format == "json":
        formatter = {
            "()": "app.observability.JsonLogFormatter",
            "message_key": settings.json_message_key,
            "level_key": settings.json_level_key,
            "logger_key": settings.json_logger_key,
            "timestamp_key": settings.json_timestamp_key,
            "exception_key": settings.json_exception_key,
            "static_fields": settings.json_static_fields,
            "include_trace_context": settings.json_include_trace_context,
        }
    else:
        formatter = {"format": settings.plaintext_format}

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": formatter},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {
                "level": settings.level,
                "handlers": ["default"],
            },
            "loggers": {
                "uvicorn": {"level": settings.level, "handlers": ["default"], "propagate": False},
                "uvicorn.error": {
                    "level": settings.level,
                    "handlers": ["default"],
                    "propagate": False,
                },
                "uvicorn.access": {
                    "level": settings.level,
                    "handlers": ["default"],
                    "propagate": False,
                },
            },
        }
    )


def configure_tracing(app: FastAPI) -> None:
    global _TRACING_INITIALIZED

    settings = load_tracing_settings_from_env()
    if not settings.enabled:
        return

    if not _TRACING_INITIALIZED:
        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.service_name})
        )
        provider.add_span_processor(BatchSpanProcessor(_build_span_exporter(settings)))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _TRACING_INITIALIZED = True
        logger.info(
            "OpenTelemetry tracing enabled: exporter=%s service_name=%s",
            settings.exporter,
            settings.service_name,
        )

    FastAPIInstrumentor.instrument_app(app, tracer_provider=trace.get_tracer_provider())


def load_logging_settings_from_env() -> LoggingSettings:
    return LoggingSettings(
        level=os.getenv("MINIGENT_LOG_LEVEL", "INFO").upper(),
        output_format=os.getenv("MINIGENT_LOG_FORMAT", "plaintext").lower(),
        plaintext_format=os.getenv("MINIGENT_LOG_PLAINTEXT_FORMAT", _DEFAULT_PLAINTEXT_FORMAT),
        json_message_key=os.getenv("MINIGENT_LOG_JSON_MESSAGE_KEY", "message"),
        json_level_key=os.getenv("MINIGENT_LOG_JSON_LEVEL_KEY", "level"),
        json_logger_key=os.getenv("MINIGENT_LOG_JSON_LOGGER_KEY", "logger"),
        json_timestamp_key=os.getenv("MINIGENT_LOG_JSON_TIMESTAMP_KEY", "timestamp"),
        json_exception_key=os.getenv("MINIGENT_LOG_JSON_EXCEPTION_KEY", "exception"),
        json_static_fields=_load_json_object_env("MINIGENT_LOG_JSON_FIELDS"),
        json_include_trace_context=_env_flag(
            "MINIGENT_LOG_JSON_INCLUDE_TRACE_CONTEXT", default=True
        ),
    )


def load_tracing_settings_from_env() -> TracingSettings:
    return TracingSettings(
        enabled=_env_flag("MINIGENT_OTEL_ENABLED", default=False),
        service_name=os.getenv("MINIGENT_OTEL_SERVICE_NAME", "minigent"),
        exporter=os.getenv("MINIGENT_OTEL_EXPORTER", "console").lower(),
        otlp_endpoint=os.getenv("MINIGENT_OTEL_EXPORTER_OTLP_ENDPOINT"),
        otlp_headers={
            key: str(value) for key, value in _load_json_object_env("MINIGENT_OTEL_EXPORTER_OTLP_HEADERS").items()
        },
        otlp_timeout_seconds=float(
            os.getenv("MINIGENT_OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS", "10")
        ),
    )


def _build_span_exporter(settings: TracingSettings) -> SpanExporter:
    if settings.exporter == "none":
        return _NoOpSpanExporter()
    if settings.exporter == "console":
        return ConsoleSpanExporter()
    if settings.exporter == "otlp":
        exporter_kwargs: dict[str, Any] = {
            "timeout": settings.otlp_timeout_seconds,
        }
        if settings.otlp_endpoint:
            exporter_kwargs["endpoint"] = settings.otlp_endpoint
        if settings.otlp_headers:
            exporter_kwargs["headers"] = settings.otlp_headers
        return OTLPSpanExporter(**exporter_kwargs)
    raise RuntimeError(
        f"Unsupported MINIGENT_OTEL_EXPORTER '{settings.exporter}'. Expected 'none', 'console', or 'otlp'."
    )


def _current_trace_context() -> dict[str, str]:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        "trace_id": f"{span_context.trace_id:032x}",
        "span_id": f"{span_context.span_id:016x}",
    }


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_json_object_env(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return parsed


class _NoOpSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        _ = spans
        return SpanExportResult.SUCCESS
