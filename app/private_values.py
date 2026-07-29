from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from fastapi import HTTPException

PII_PLACEHOLDER_PATTERN = re.compile(
    r"\{\{pii:(?P<kind>[a-z][a-z0-9_]*):(?P<reference>[A-Za-z0-9_-]+)\}\}"
)
DEFAULT_PRIVATE_VALUE_TTL_SECONDS = 1800.0
DEFAULT_PRIVATE_VALUE_MAX_REFS_PER_THREAD = 1000
DEFAULT_PRIVATE_VALUE_MAX_CHARS = 10_000
PRIVATE_VALUE_TTL_SECONDS_ENV = "MINIGENT_PRIVATE_VALUE_TTL_SECONDS"
PRIVATE_VALUE_MAX_REFS_ENV = "MINIGENT_PRIVATE_VALUE_MAX_REFS_PER_THREAD"
PRIVATE_VALUE_MAX_CHARS_ENV = "MINIGENT_PRIVATE_VALUE_MAX_CHARS"


@dataclass(frozen=True)
class PrivateValueEntry:
    value: str
    expires_at: float


class InMemoryPrivateValueStore:
    """Bounded, expiring private values used to render model-safe placeholders."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PRIVATE_VALUE_TTL_SECONDS,
        max_refs_per_thread: int = DEFAULT_PRIVATE_VALUE_MAX_REFS_PER_THREAD,
        max_value_chars: int = DEFAULT_PRIVATE_VALUE_MAX_CHARS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("private value TTL must be positive")
        if max_refs_per_thread < 1:
            raise ValueError("private value reference limit must be positive")
        if max_value_chars < 1:
            raise ValueError("private value character limit must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_refs_per_thread = max_refs_per_thread
        self._max_value_chars = max_value_chars
        self._clock = clock
        self._values: dict[tuple[str, str], dict[str, PrivateValueEntry]] = {}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> InMemoryPrivateValueStore:
        lookup = os.environ if env is None else env
        return cls(
            ttl_seconds=_positive_float_setting(
                lookup,
                PRIVATE_VALUE_TTL_SECONDS_ENV,
                DEFAULT_PRIVATE_VALUE_TTL_SECONDS,
            ),
            max_refs_per_thread=_positive_int_setting(
                lookup,
                PRIVATE_VALUE_MAX_REFS_ENV,
                DEFAULT_PRIVATE_VALUE_MAX_REFS_PER_THREAD,
            ),
            max_value_chars=_positive_int_setting(
                lookup,
                PRIVATE_VALUE_MAX_CHARS_ENV,
                DEFAULT_PRIVATE_VALUE_MAX_CHARS,
            ),
        )

    def add(
        self,
        tenant_id: str,
        thread_id: str,
        values: Mapping[str, str],
    ) -> None:
        key = (tenant_id, thread_id)
        self._prune_thread(key)
        thread_values = self._values.setdefault(key, {})
        new_references = set(values) - set(thread_values)
        if len(thread_values) + len(new_references) > self._max_refs_per_thread:
            raise HTTPException(
                status_code=502,
                detail="Private value reference limit exceeded for thread",
            )
        expires_at = self._clock() + self._ttl_seconds
        for reference, value in values.items():
            if len(value) > self._max_value_chars:
                raise HTTPException(
                    status_code=502,
                    detail="Private value exceeded the configured character limit",
                )
            existing = thread_values.get(reference)
            if existing is not None and existing.value != value:
                raise HTTPException(
                    status_code=502,
                    detail=f"Private value reference collision for '{reference}'",
                )
            thread_values[reference] = PrivateValueEntry(value=value, expires_at=expires_at)

    def render_for_user(self, tenant_id: str, thread_id: str, text: str) -> str:
        key = (tenant_id, thread_id)
        self._prune_thread(key)
        thread_values = self._values.get(key, {})

        def replace(match: re.Match[str]) -> str:
            entry = thread_values.get(match.group("reference"))
            return entry.value if entry is not None else match.group(0)

        return PII_PLACEHOLDER_PATTERN.sub(replace, text)

    def clear_thread(self, tenant_id: str, thread_id: str) -> None:
        self._values.pop((tenant_id, thread_id), None)

    def _prune_thread(self, key: tuple[str, str]) -> None:
        thread_values = self._values.get(key)
        if not thread_values:
            return
        now = self._clock()
        expired = [
            reference for reference, entry in thread_values.items() if entry.expires_at <= now
        ]
        for reference in expired:
            thread_values.pop(reference, None)
        if not thread_values:
            self._values.pop(key, None)


def _positive_float_setting(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def _positive_int_setting(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value
