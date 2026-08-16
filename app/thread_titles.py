from __future__ import annotations

import re

DEFAULT_THREAD_TITLE = "New conversation"
GENERATED_THREAD_TITLE_LIMIT = 64
MANUAL_THREAD_TITLE_LIMIT = 120

_LEADING_REQUEST_PATTERNS = (
    re.compile(r"^please\s+", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?(?:can|could|would|will)\s+you\s+", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?(?:help\s+(?:me\s+)?(?:to\s+)?)", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?i(?:'d|\s+would)\s+like\s+(?:you\s+)?to\s+", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?i\s+want\s+(?:you\s+)?to\s+", re.IGNORECASE),
)
_LEADING_ACTION_PATTERNS = (
    (re.compile(r"^(?:please\s+)?take\s+a\s+look\s+at\s+", re.IGNORECASE), "Investigate "),
    (re.compile(r"^(?:please\s+)?look\s+into\s+", re.IGNORECASE), "Investigate "),
    (re.compile(r"^(?:please\s+)?check\s+(?:out\s+)?", re.IGNORECASE), "Review "),
)
_TRAILING_POLITENESS = re.compile(
    r"(?:,?\s+please|\s+thanks?|\s+thank\s+you)[.!?]*$", re.IGNORECASE
)


def generate_thread_title(content: str, limit: int = GENERATED_THREAD_TITLE_LIMIT) -> str:
    """Build a compact, deterministic title without conversational request boilerplate."""
    title = " ".join(content.split()).strip()
    if not title:
        return DEFAULT_THREAD_TITLE

    previous = None
    while title != previous:
        previous = title
        for pattern in _LEADING_REQUEST_PATTERNS:
            title = pattern.sub("", title, count=1).strip()
        for pattern, replacement in _LEADING_ACTION_PATTERNS:
            title = pattern.sub(replacement, title, count=1).strip()

    title = _TRAILING_POLITENESS.sub("", title).strip(" \t\r\n.!?")
    if not title:
        return DEFAULT_THREAD_TITLE
    title = title[0].upper() + title[1:]
    if len(title) <= limit:
        return title
    return f"{title[: limit - 1].rstrip()}…"


def normalize_manual_thread_title(title: str) -> str:
    normalized = " ".join(title.split()).strip()
    if not normalized:
        raise ValueError("thread title is required")
    if len(normalized) > MANUAL_THREAD_TITLE_LIMIT:
        raise ValueError(f"thread title must be {MANUAL_THREAD_TITLE_LIMIT} characters or fewer")
    return normalized
