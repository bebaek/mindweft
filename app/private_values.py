from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import HTTPException

PII_PLACEHOLDER_PATTERN = re.compile(
    r"\{\{pii:(?P<kind>[a-z][a-z0-9_]*):(?P<reference>[A-Za-z0-9_-]+)\}\}"
)


class InMemoryPrivateValueStore:
    """Thread-scoped private values used to render model-safe placeholders for a user."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], dict[str, str]] = {}

    def add(
        self,
        tenant_id: str,
        thread_id: str,
        values: Mapping[str, str],
    ) -> None:
        thread_values = self._values.setdefault((tenant_id, thread_id), {})
        for reference, value in values.items():
            existing = thread_values.get(reference)
            if existing is not None and existing != value:
                raise HTTPException(
                    status_code=502,
                    detail=f"Private value reference collision for '{reference}'",
                )
            thread_values[reference] = value

    def render_for_user(self, tenant_id: str, thread_id: str, text: str) -> str:
        thread_values = self._values.get((tenant_id, thread_id), {})

        def replace(match: re.Match[str]) -> str:
            return thread_values.get(match.group("reference"), match.group(0))

        return PII_PLACEHOLDER_PATTERN.sub(replace, text)
