from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "authorization",
    "credential",
    "private_key",
    "access_key",
    "refresh_token",
    "key",
)
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
_SECRET_TEXT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)
_LOG_REDACTION_FACTORY: Callable[..., logging.LogRecord] | None = None
TOOL_RESULT_REDACTION_BEST_EFFORT = "best_effort"
TOOL_RESULT_REDACTION_NONE = "none"
TOOL_RESULT_REDACTION_FULL = "full"
TOOL_RESULT_REDACTION_MODES = {
    TOOL_RESULT_REDACTION_BEST_EFFORT,
    TOOL_RESULT_REDACTION_NONE,
    TOOL_RESULT_REDACTION_FULL,
}


@dataclass(frozen=True)
class ToolResultRedactionPolicy:
    enabled: bool = True
    mode: str = TOOL_RESULT_REDACTION_BEST_EFFORT
    sensitive_tools: frozenset[str] = field(default_factory=frozenset)


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


def redact_secret_patterns_in_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def sanitize_tool_result(
    value: Any,
    *,
    policy: ToolResultRedactionPolicy | None = None,
    tool_name: str | None = None,
) -> Any:
    """Return a redacted copy of a tool result according to policy.

    Tool outputs can be persisted in thread history, streamed to clients, and supplied to
    later LLM turns. Keep this sanitizer conservative and deterministic so the value
    leaving the tool-management layer is safe for those downstream consumers.
    """
    effective_policy = policy or ToolResultRedactionPolicy()
    if not effective_policy.enabled or effective_policy.mode == TOOL_RESULT_REDACTION_NONE:
        return value
    if effective_policy.mode == TOOL_RESULT_REDACTION_FULL or (
        tool_name is not None and tool_name in effective_policy.sensitive_tools
    ):
        return _fully_redacted_tool_result(value, tool_name=tool_name)
    return _sanitize_tool_result_value("", value)


def _fully_redacted_tool_result(value: Any, *, tool_name: str | None) -> dict[str, Any]:
    return {
        "redacted": True,
        "tool_name": tool_name,
        "result_type": type(value).__name__,
        "note": "Tool result redacted by Minigent policy.",
    }


def parse_tool_result_redaction_policy(
    raw: Any,
    *,
    context: str,
    default: ToolResultRedactionPolicy | None = None,
) -> ToolResultRedactionPolicy:
    base = default or ToolResultRedactionPolicy()
    if raw is None:
        return base
    if isinstance(raw, bool):
        return ToolResultRedactionPolicy(
            enabled=raw,
            mode=base.mode,
            sensitive_tools=base.sensitive_tools,
        )
    if not isinstance(raw, dict):
        raise RuntimeError(f"{context} result_redaction must be an object or boolean")
    enabled = raw.get("enabled", base.enabled)
    if not isinstance(enabled, bool):
        raise RuntimeError(f"{context} result_redaction.enabled must be boolean")
    mode = str(raw.get("mode", base.mode)).strip().lower()
    if mode not in TOOL_RESULT_REDACTION_MODES:
        raise RuntimeError(
            f"{context} result_redaction.mode must be one of: "
            f"{', '.join(sorted(TOOL_RESULT_REDACTION_MODES))}"
        )
    sensitive_tools_raw = raw.get("sensitive_tools", raw.get("sensitiveTools"))
    if sensitive_tools_raw is None:
        sensitive_tools = base.sensitive_tools
    else:
        if not isinstance(sensitive_tools_raw, list) or not all(
            isinstance(item, str) and item for item in sensitive_tools_raw
        ):
            raise RuntimeError(
                f"{context} result_redaction.sensitive_tools must be an array of strings"
            )
        sensitive_tools = frozenset(sensitive_tools_raw)
    return ToolResultRedactionPolicy(
        enabled=enabled,
        mode=mode,
        sensitive_tools=sensitive_tools,
    )


def _sanitize_tool_result_value(key: str, value: Any) -> Any:
    if is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, str):
        return redact_secret_patterns_in_text(redact_urls_in_text(value))
    if _looks_like_url_value(value):
        return redact_urls_in_text(str(value))
    if isinstance(value, dict):
        return {
            nested_key: _sanitize_tool_result_value(str(nested_key), nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_tool_result_value(key, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_tool_result_value(key, item) for item in value)
    return value


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
