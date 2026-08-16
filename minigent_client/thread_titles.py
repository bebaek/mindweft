from __future__ import annotations

import re

THREAD_TITLE_LIMIT = 64
_PLACEHOLDER_TITLES = {"thread", "new thread", "new conversation", "untitled thread"}

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


def is_placeholder_thread_title(title: str | None) -> bool:
    return not title or title.strip().casefold() in _PLACEHOLDER_TITLES


def thread_title_from_message(message: str, limit: int = THREAD_TITLE_LIMIT) -> str:
    title = " ".join(message.split()).strip()
    if not title:
        return "New conversation"

    previous = None
    while title != previous:
        previous = title
        for pattern in _LEADING_REQUEST_PATTERNS:
            title = pattern.sub("", title, count=1).strip()
        for pattern, replacement in _LEADING_ACTION_PATTERNS:
            title = pattern.sub(replacement, title, count=1).strip()

    title = _TRAILING_POLITENESS.sub("", title).strip(" \t\r\n.!?")
    if not title:
        return "New conversation"
    title = title[0].upper() + title[1:]
    if len(title) <= limit:
        return title
    return f"{title[: limit - 1].rstrip()}…"
