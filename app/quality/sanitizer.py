from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Redaction:
    kind: str
    original_hash: str
    replacement: str
    confidence: float = 1.0


@dataclass(frozen=True)
class SanitizedPayload:
    text: str
    redactions: list[Redaction]
    blocked: bool = False
    block_reason: str | None = None


_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (
        "api_key",
        re.compile(r"\b(?:sk|pk|rk|api)[-_][A-Za-z0-9_-]{20,}\b", re.IGNORECASE),
        "[REDACTED_API_KEY]",
    ),
    (
        "assignment_secret",
        re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
        "[REDACTED_SECRET_ASSIGNMENT]",
    ),
]
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w.-])(?:/Users/[^\s:'\")]+|/home/[^\s:'\")]+|/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){2,})"
)
_INTERNAL_URL_PATTERN = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|[^\s/'\"]*\.local)(?:[^\s'\"]*)?",
    re.IGNORECASE,
)


def sanitize_for_remote(text: str, *, max_chars: int = 6000) -> SanitizedPayload:
    redactions: list[Redaction] = []
    sanitized = text
    for kind, pattern, replacement in _SECRET_PATTERNS:
        sanitized, new_redactions = _substitute(pattern, sanitized, kind, replacement)
        redactions.extend(new_redactions)
    sanitized, email_redactions = _substitute(_EMAIL_PATTERN, sanitized, "email", "[EMAIL]")
    redactions.extend(email_redactions)
    sanitized, url_redactions = _substitute(
        _INTERNAL_URL_PATTERN, sanitized, "internal_url", "[INTERNAL_URL]"
    )
    redactions.extend(url_redactions)
    sanitized, path_redactions = _substitute(
        _ABSOLUTE_PATH_PATTERN, sanitized, "absolute_path", "[PATH]"
    )
    redactions.extend(path_redactions)

    if len(sanitized) > max_chars:
        sanitized = sanitized[:max_chars].rstrip() + "\n[TRUNCATED]"
        redactions.append(Redaction(kind="truncation", original_hash="", replacement="[TRUNCATED]"))

    if _private_key_marker_remaining(sanitized):
        return SanitizedPayload(
            text=sanitized,
            redactions=redactions,
            blocked=True,
            block_reason="private key material may remain after sanitization",
        )
    return SanitizedPayload(text=sanitized, redactions=redactions)


def _substitute(
    pattern: re.Pattern[str],
    value: str,
    kind: str,
    replacement: str,
) -> tuple[str, list[Redaction]]:
    redactions: list[Redaction] = []

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        redactions.append(
            Redaction(
                kind=kind,
                original_hash=hashlib.sha256(original.encode("utf-8")).hexdigest(),
                replacement=replacement,
            )
        )
        return replacement

    return pattern.sub(replace, value), redactions


def _private_key_marker_remaining(value: str) -> bool:
    return "-----BEGIN" in value or "-----END" in value
