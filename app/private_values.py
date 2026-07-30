from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

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
PRIVATE_VALUE_DB_PATH_ENV = "MINIGENT_PRIVATE_VALUE_DB_PATH"
PRIVATE_VALUE_ENCRYPTION_KEY_ENV = "MINIGENT_PRIVATE_VALUE_ENCRYPTION_KEY"
PRIVATE_VALUE_ENCRYPTION_KEYS_ENV = "MINIGENT_PRIVATE_VALUE_ENCRYPTION_KEYS"
PRIVATE_VALUE_KEY_VERSION_ENV = "MINIGENT_PRIVATE_VALUE_KEY_VERSION"
PRIVATE_VALUE_REENCRYPT_ON_STARTUP_ENV = "MINIGENT_PRIVATE_VALUE_REENCRYPT_ON_STARTUP"
INPUT_PII_PROTECTION_ENABLED_ENV = "MINIGENT_INPUT_PII_PROTECTION_ENABLED"

_EMAIL_PATTERN = re.compile(
    r"(?<![\w@])(?P<value>[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)(?![\w@])",
    re.IGNORECASE,
)
_PHONE_PATTERN = re.compile(
    r"(?<!\w)(?P<value>(?:\+?\d{1,3}[ .-]?)?"
    r"(?:\(?\d{2,4}\)?[ .-]?)?\d{3}[ .-]?\d{4})(?!\w)"
)
_ADDRESS_PATTERN = re.compile(
    r"(?<!\w)(?P<value>\d{1,6}\s+"
    r"(?:[A-Z][A-Z0-9.'-]*\s+){1,6}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Court|Ct|Way|Parkway|Pkwy|Place|Pl)\.?"
    r"(?:,?\s+(?:Apt|Apartment|Unit|Suite|Ste|#)\s*[A-Z0-9-]+)?"
    r"(?:,\s*[A-Z][A-Z.'-]*(?:\s+[A-Z][A-Z.'-]*){0,3},?\s+"
    r"[A-Z]{2}\s+\d{5}(?:-\d{4})?)?)",
    re.IGNORECASE,
)
_NAME_TOKEN = r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?"
_TITLED_PERSON_PATTERN = re.compile(
    rf"(?<!\w)(?P<value>(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof)\.?\s+"
    rf"{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,3}})(?!\w)"
)
_POSSESSIVE_PERSON_PATTERN = re.compile(
    rf"(?<!\w)(?P<value>{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{1,3}})(?=['’]s\b)"
)
_CONTEXT_PERSON_PATTERN = re.compile(
    rf"(?i:\b(?:call|email|contact|ask|tell|message|meet|with|for|about)\s+)"
    rf"(?P<value>{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,3}})(?!\w)"
)
_CONTACT_NAMED_PERSON_PATTERN = re.compile(
    rf"(?i:\bcontact\s+(?:named|called)\s+)"
    rf"(?P<value>{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,3}})(?!\w)"
)
_CONTACT_CREATE_PERSON_PATTERN = re.compile(
    rf"(?i:\b(?:add|create|save)\s+)"
    rf"(?P<value>{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,3}})"
    rf"(?i:\s+(?:as\s+)?(?:a\s+)?(?:new\s+)?contact\b)"
)


class PrivateValueStore(Protocol):
    def add(
        self,
        tenant_id: str,
        thread_id: str,
        values: Mapping[str, str],
        *,
        user_id: str = "",
        kinds: Mapping[str, str] | None = None,
    ) -> None: ...

    def render_for_user(
        self, tenant_id: str, thread_id: str, text: str, *, user_id: str = ""
    ) -> str: ...

    def validate_for_tool(
        self, tenant_id: str, thread_id: str, text: str, *, user_id: str = ""
    ) -> None: ...

    def resolve_for_tool(
        self, tenant_id: str, thread_id: str, text: str, *, user_id: str = ""
    ) -> str: ...

    def clear_thread(self, tenant_id: str, thread_id: str) -> None: ...


@dataclass(frozen=True)
class ProtectedText:
    text: str
    private_values: dict[str, str]
    private_value_kinds: dict[str, str]


class LocalPIIProtector:
    """Conservative local regex protection for common PII in user-authored text."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        reference_factory: Callable[[], str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._reference_factory = reference_factory or (
            lambda: f"local-{secrets.token_urlsafe(12)}"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LocalPIIProtector:
        lookup = os.environ if env is None else env
        raw = lookup.get(INPUT_PII_PROTECTION_ENABLED_ENV, "true").strip().lower()
        if raw not in {"true", "false"}:
            raise RuntimeError(f"{INPUT_PII_PROTECTION_ENABLED_ENV} must be true or false")
        return cls(enabled=raw == "true")

    def protect(self, text: str) -> ProtectedText:
        if not self._enabled or not text:
            return ProtectedText(text=text, private_values={}, private_value_kinds={})

        matches: list[tuple[int, int, str, str]] = []
        protected_ranges = [match.span() for match in PII_PLACEHOLDER_PATTERN.finditer(text)]
        detectors = (
            ("email", _EMAIL_PATTERN, 0),
            ("address", _ADDRESS_PATTERN, 1),
            ("phone", _PHONE_PATTERN, 2),
            ("person", _TITLED_PERSON_PATTERN, 3),
            ("person", _POSSESSIVE_PERSON_PATTERN, 4),
            ("person", _CONTEXT_PERSON_PATTERN, 5),
            ("person", _CONTACT_NAMED_PERSON_PATTERN, 6),
            ("person", _CONTACT_CREATE_PERSON_PATTERN, 7),
        )
        candidates: list[tuple[int, int, int, str, str]] = []
        for kind, pattern, priority in detectors:
            for match in pattern.finditer(text):
                start, end = match.span("value")
                value = match.group("value")
                if kind == "phone" and sum(character.isdigit() for character in value) < 7:
                    continue
                if any(
                    start < range_end and end > range_start
                    for range_start, range_end in protected_ranges
                ):
                    continue
                candidates.append((priority, -(end - start), start, kind, value))

        occupied: list[tuple[int, int]] = []
        for _priority, _negative_length, start, kind, value in sorted(candidates):
            end = start + len(value)
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            occupied.append((start, end))
            matches.append((start, end, kind, value))

        private_values: dict[str, str] = {}
        private_value_kinds: dict[str, str] = {}
        replacements: dict[tuple[str, str], str] = {}
        protected_text = text
        for start, end, kind, value in sorted(matches, reverse=True):
            key = (kind, value)
            reference = replacements.get(key)
            if reference is None:
                reference = self._reference_factory()
                replacements[key] = reference
                private_values[reference] = value
                private_value_kinds[reference] = kind
            placeholder = f"{{{{pii:{kind}:{reference}}}}}"
            protected_text = protected_text[:start] + placeholder + protected_text[end:]
        return ProtectedText(
            text=protected_text,
            private_values=private_values,
            private_value_kinds=private_value_kinds,
        )


@dataclass(frozen=True)
class PrivateValueEntry:
    value: str
    kind: str
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
        self._values: dict[tuple[str, str, str], dict[str, PrivateValueEntry]] = {}

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
        *,
        user_id: str = "",
        kinds: Mapping[str, str] | None = None,
    ) -> None:
        key = (tenant_id, user_id, thread_id)
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
            requested_kind = (kinds or {}).get(reference, "unknown")
            kind = requested_kind if re.fullmatch(r"[a-z][a-z0-9_]*", requested_kind) else "unknown"
            existing = thread_values.get(reference)
            if existing is not None:
                if existing.value != value:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Private value reference collision for '{reference}'",
                    )
                if existing.kind != "unknown" and kind != "unknown" and existing.kind != kind:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Private value kind collision for '{reference}'",
                    )
                if kind == "unknown":
                    kind = existing.kind
            thread_values[reference] = PrivateValueEntry(
                value=value,
                kind=kind,
                expires_at=expires_at,
            )

    def render_for_user(
        self, tenant_id: str, thread_id: str, text: str, *, user_id: str = ""
    ) -> str:
        key = (tenant_id, user_id, thread_id)
        self._prune_thread(key)
        thread_values = self._values.get(key, {})

        def replace(match: re.Match[str]) -> str:
            entry = thread_values.get(match.group("reference"))
            if entry is None or (entry.kind != "unknown" and entry.kind != match.group("kind")):
                return match.group(0)
            return entry.value

        return PII_PLACEHOLDER_PATTERN.sub(replace, text)

    def validate_for_tool(
        self, tenant_id: str, thread_id: str, text: str, *, user_id: str = ""
    ) -> None:
        key = (tenant_id, user_id, thread_id)
        self._prune_thread(key)
        thread_values = self._values.get(key, {})
        for match in PII_PLACEHOLDER_PATTERN.finditer(text):
            entry = thread_values.get(match.group("reference"))
            if entry is None:
                raise HTTPException(
                    status_code=409,
                    detail="Private value is missing or expired",
                )
            if entry.kind != "unknown" and entry.kind != match.group("kind"):
                raise HTTPException(
                    status_code=409,
                    detail="Private value kind does not match placeholder",
                )

    def resolve_for_tool(
        self, tenant_id: str, thread_id: str, text: str, *, user_id: str = ""
    ) -> str:
        self.validate_for_tool(tenant_id, thread_id, text, user_id=user_id)
        thread_values = self._values.get((tenant_id, user_id, thread_id), {})

        def replace(match: re.Match[str]) -> str:
            return thread_values[match.group("reference")].value

        return PII_PLACEHOLDER_PATTERN.sub(replace, text)

    def clear_thread(self, tenant_id: str, thread_id: str) -> None:
        keys = [key for key in self._values if key[0] == tenant_id and key[2] == thread_id]
        for key in keys:
            self._values.pop(key, None)

    def _prune_thread(self, key: tuple[str, str, str]) -> None:
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


def build_private_value_store_from_env(
    env: Mapping[str, str] | None = None,
) -> PrivateValueStore:
    lookup = os.environ if env is None else env
    db_path = lookup.get(PRIVATE_VALUE_DB_PATH_ENV, "").strip()
    if not db_path:
        return InMemoryPrivateValueStore.from_env(lookup)
    from app.private_value_sqlite import SQLiteEncryptedPrivateValueStore

    return SQLiteEncryptedPrivateValueStore.from_env(lookup)


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
