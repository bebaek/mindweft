import json
import logging

from fastapi import FastAPI

from app.observability import (
    JsonLogFormatter,
    configure_tracing,
    load_logging_settings_from_env,
    load_tracing_settings_from_env,
)


def test_load_logging_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_LOG_FORMAT", "json")
    monkeypatch.setenv("MINIGENT_LOG_JSON_ROOT_KEY", "log")
    monkeypatch.setenv("MINIGENT_LOG_JSON_MESSAGE_KEY", "msg")
    monkeypatch.setenv("MINIGENT_LOG_JSON_FIELDS", '{"service":"minigent","env":"test"}')

    settings = load_logging_settings_from_env()

    assert settings.output_format == "json"
    assert settings.json_root_key == "log"
    assert settings.json_message_key == "msg"
    assert settings.json_static_fields == {"service": "minigent", "env": "test"}


def test_json_log_formatter_uses_configured_keys() -> None:
    formatter = JsonLogFormatter(
        root_key=None,
        message_key="msg",
        level_key="severity",
        logger_key="source",
        timestamp_key="ts",
        exception_key="error",
        static_fields={"service": "minigent"},
        include_trace_context=False,
    )
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["msg"] == "hello world"
    assert payload["severity"] == "INFO"
    assert payload["source"] == "app.test"
    assert payload["service"] == "minigent"
    assert "ts" in payload


def test_json_log_formatter_can_nest_payload_under_root_key() -> None:
    formatter = JsonLogFormatter(
        root_key="log",
        message_key="message",
        level_key="level",
        logger_key="logger",
        timestamp_key="timestamp",
        exception_key="exception",
        static_fields={"service": "minigent"},
        include_trace_context=False,
    )
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["service"] == "minigent"
    assert payload["log"]["message"] == "hello"
    assert payload["log"]["level"] == "INFO"


def test_load_tracing_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_OTEL_ENABLED", "true")
    monkeypatch.setenv("MINIGENT_OTEL_EXPORTER", "otlp")
    monkeypatch.setenv("MINIGENT_OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.example/v1/traces")
    monkeypatch.setenv("MINIGENT_OTEL_EXPORTER_OTLP_HEADERS", '{"authorization":"Bearer token"}')

    settings = load_tracing_settings_from_env()

    assert settings.enabled is True
    assert settings.exporter == "otlp"
    assert settings.otlp_endpoint == "https://otel.example/v1/traces"
    assert settings.otlp_headers == {"authorization": "Bearer token"}


def test_configure_tracing_instruments_fastapi_and_httpx(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_OTEL_ENABLED", "true")
    monkeypatch.setenv("MINIGENT_OTEL_EXPORTER", "console")

    calls: list[str] = []

    class FakeProvider:
        def __init__(self, *, resource) -> None:
            calls.append(f"provider:{resource}")

        def add_span_processor(self, processor) -> None:
            calls.append(f"processor:{type(processor).__name__}")

    class FakeResource:
        @staticmethod
        def create(attributes):
            calls.append(f"resource:{attributes['service.name']}")
            return attributes

    class FakeHTTPXInstrumentor:
        def instrument(self) -> None:
            calls.append("httpx")

    class FakeFastAPIInstrumentor:
        @staticmethod
        def instrument_app(app, tracer_provider) -> None:
            assert isinstance(app, FastAPI)
            calls.append(f"fastapi:{type(tracer_provider).__name__}")

    monkeypatch.setattr("app.observability.TracerProvider", FakeProvider)
    monkeypatch.setattr("app.observability.Resource", FakeResource)
    monkeypatch.setattr(
        "app.observability.BatchSpanProcessor",
        lambda exporter: type("Processor", (), {"exporter": exporter})(),
    )
    monkeypatch.setattr(
        "app.observability.ConsoleSpanExporter",
        lambda: type("ConsoleExporter", (), {})(),
    )
    monkeypatch.setattr("app.observability.HTTPXClientInstrumentor", lambda: FakeHTTPXInstrumentor())
    monkeypatch.setattr("app.observability.FastAPIInstrumentor", FakeFastAPIInstrumentor)

    tracer_provider_holder: dict[str, object] = {}

    def fake_set_tracer_provider(provider) -> None:
        tracer_provider_holder["provider"] = provider

    def fake_get_tracer_provider():
        return tracer_provider_holder["provider"]

    monkeypatch.setattr("app.observability.trace.set_tracer_provider", fake_set_tracer_provider)
    monkeypatch.setattr("app.observability.trace.get_tracer_provider", fake_get_tracer_provider)
    monkeypatch.setattr("app.observability._TRACING_INITIALIZED", False)

    configure_tracing(FastAPI())

    assert "resource:minigent" in calls
    assert "httpx" in calls
    assert "fastapi:FakeProvider" in calls
