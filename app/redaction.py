from __future__ import annotations

import logging
import re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "api_key", "authorization", "key")
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
_LOG_REDACTION_FACTORY: Callable[..., logging.LogRecord] | None = None


def is_sensitive_key(key: str) -> bool:
    lowercase_key = key.lower()
    return any(secret_key in lowercase_key for secret_key in _SENSITIVE_KEY_PARTS)


def redact_url_secrets(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc or not parts.query:
        return value

    query_items = parse_qsl(parts.query, keep_blank_values=True)
    redacted = False
    sanitized_items: list[tuple[str, str]] = []
    for key, item_value in query_items:
        if is_sensitive_key(key):
            sanitized_items.append((key, "<redacted>"))
            redacted = True
        else:
            sanitized_items.append((key, item_value))

    if not redacted:
        return value

    return urlunsplit(parts._replace(query=urlencode(sanitized_items)))


def redact_urls_in_text(value: str) -> str:
    return _URL_PATTERN.sub(lambda match: redact_url_secrets(match.group(0)), value)


def sanitize_value_for_logging(key: str, value: Any) -> Any:
    if is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, str):
        value = redact_urls_in_text(value)
        if len(value) > 200:
            return f"{value[:200]}...<truncated>"
        return value
    if _looks_like_url_value(value):
        return redact_urls_in_text(str(value))
    if isinstance(value, dict):
        return {
            nested_key: sanitize_value_for_logging(nested_key, nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value_for_logging(key, item) for item in value[:20]]
    return value


def _looks_like_url_value(value: Any) -> bool:
    try:
        string_value = str(value)
    except Exception:
        return False
    return string_value.startswith(("http://", "https://"))


class RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        redact_log_record(record)
        return True


def redact_log_record(record: logging.LogRecord) -> None:
    if isinstance(record.msg, str):
        record.msg = redact_urls_in_text(record.msg)
    if isinstance(record.args, dict):
        record.args = {
            key: sanitize_value_for_logging(key, value) for key, value in record.args.items()
        }
    elif isinstance(record.args, tuple):
        record.args = tuple(sanitize_value_for_logging("", value) for value in record.args)


def install_log_redaction() -> None:
    global _LOG_REDACTION_FACTORY
    current_factory = logging.getLogRecordFactory()
    if _LOG_REDACTION_FACTORY is not None and current_factory is _LOG_REDACTION_FACTORY:
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current_factory(*args, **kwargs)
        redact_log_record(record)
        return record

    logging.setLogRecordFactory(redacting_factory)
    _LOG_REDACTION_FACTORY = redacting_factory
