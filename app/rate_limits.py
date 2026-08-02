from __future__ import annotations

import math
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

RATE_LIMIT_DB_PATH_ENV = "MINIGENT_RATE_LIMIT_DB_PATH"
UPLOAD_TENANT_CAPACITY_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_CAPACITY"
UPLOAD_TENANT_REFILL_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_REFILL_PER_SECOND"
UPLOAD_USER_CAPACITY_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_USER_CAPACITY"
UPLOAD_USER_REFILL_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_USER_REFILL_PER_SECOND"
RUN_TENANT_CAPACITY_ENV = "MINIGENT_RUN_RATE_LIMIT_TENANT_CAPACITY"
RUN_TENANT_REFILL_ENV = "MINIGENT_RUN_RATE_LIMIT_TENANT_REFILL_PER_SECOND"
RUN_USER_CAPACITY_ENV = "MINIGENT_RUN_RATE_LIMIT_USER_CAPACITY"
RUN_USER_REFILL_ENV = "MINIGENT_RUN_RATE_LIMIT_USER_REFILL_PER_SECOND"
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 1.0 / 60.0
MAX_RETRY_AFTER_SECONDS = 86_400


@dataclass(frozen=True)
class RateLimitPolicy:
    tenant_capacity: int = 0
    tenant_refill_per_second: float = DEFAULT_RATE_LIMIT_REFILL_PER_SECOND
    user_capacity: int = 0
    user_refill_per_second: float = DEFAULT_RATE_LIMIT_REFILL_PER_SECOND

    @property
    def enabled(self) -> bool:
        return self.tenant_capacity > 0 or self.user_capacity > 0


@dataclass(frozen=True)
class RateLimitSettings:
    db_path: str | None = None
    uploads: RateLimitPolicy = RateLimitPolicy()
    runs: RateLimitPolicy = RateLimitPolicy()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RateLimitSettings:
        lookup = os.environ if env is None else env
        return cls(
            db_path=lookup.get(RATE_LIMIT_DB_PATH_ENV, "").strip() or None,
            uploads=_policy_from_env(
                lookup,
                tenant_capacity_env=UPLOAD_TENANT_CAPACITY_ENV,
                tenant_refill_env=UPLOAD_TENANT_REFILL_ENV,
                user_capacity_env=UPLOAD_USER_CAPACITY_ENV,
                user_refill_env=UPLOAD_USER_REFILL_ENV,
            ),
            runs=_policy_from_env(
                lookup,
                tenant_capacity_env=RUN_TENANT_CAPACITY_ENV,
                tenant_refill_env=RUN_TENANT_REFILL_ENV,
                user_capacity_env=RUN_USER_CAPACITY_ENV,
                user_refill_env=RUN_USER_REFILL_ENV,
            ),
        )


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    rejected_scope: str | None = None


class RateLimiter(Protocol):
    def consume(
        self,
        category: str,
        tenant_id: str,
        user_id: str,
        policy: RateLimitPolicy,
        *,
        now: float | None = None,
    ) -> RateLimitDecision: ...


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass(frozen=True)
class _BucketSpec:
    key: tuple[str, str, str, str]
    capacity: int
    refill_per_second: float


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, str, str], _Bucket] = {}
        self._lock = RLock()

    def consume(
        self,
        category: str,
        tenant_id: str,
        user_id: str,
        policy: RateLimitPolicy,
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        specs = _bucket_specs(category, tenant_id, user_id, policy)
        if not specs:
            return RateLimitDecision(allowed=True)
        timestamp = time.time() if now is None else now
        with self._lock:
            states = [
                _refilled_bucket(
                    self._buckets.get(spec.key),
                    spec.capacity,
                    spec.refill_per_second,
                    timestamp,
                )
                for spec in specs
            ]
            rejected = _rejected_decision(specs, states)
            if rejected is not None:
                return rejected
            for spec, state in zip(specs, states, strict=True):
                self._buckets[spec.key] = _Bucket(
                    tokens=state.tokens - 1.0,
                    updated_at=state.updated_at,
                )
        return RateLimitDecision(allowed=True)


class SQLiteRateLimiter:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = RLock()
        Path(self._db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                    category TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    tokens REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (category, scope, tenant_id, user_id)
                )
                """
            )

    def consume(
        self,
        category: str,
        tenant_id: str,
        user_id: str,
        policy: RateLimitPolicy,
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        specs = _bucket_specs(category, tenant_id, user_id, policy)
        if not specs:
            return RateLimitDecision(allowed=True)
        timestamp = time.time() if now is None else now
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            states = []
            for spec in specs:
                row = connection.execute(
                    """
                    SELECT tokens, updated_at
                    FROM rate_limit_buckets
                    WHERE category = ? AND scope = ? AND tenant_id = ? AND user_id = ?
                    """,
                    spec.key,
                ).fetchone()
                bucket = _Bucket(float(row[0]), float(row[1])) if row is not None else None
                states.append(
                    _refilled_bucket(
                        bucket,
                        spec.capacity,
                        spec.refill_per_second,
                        timestamp,
                    )
                )
            rejected = _rejected_decision(specs, states)
            if rejected is not None:
                return rejected
            for spec, state in zip(specs, states, strict=True):
                connection.execute(
                    """
                    INSERT INTO rate_limit_buckets (
                        category, scope, tenant_id, user_id, tokens, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category, scope, tenant_id, user_id) DO UPDATE SET
                        tokens = excluded.tokens,
                        updated_at = excluded.updated_at
                    """,
                    (*spec.key, state.tokens - 1.0, state.updated_at),
                )
        return RateLimitDecision(allowed=True)


def build_rate_limiter(settings: RateLimitSettings) -> RateLimiter:
    if settings.db_path is not None:
        return SQLiteRateLimiter(settings.db_path)
    return InMemoryRateLimiter()


def _bucket_specs(
    category: str,
    tenant_id: str,
    user_id: str,
    policy: RateLimitPolicy,
) -> tuple[_BucketSpec, ...]:
    specs = []
    if policy.tenant_capacity > 0:
        specs.append(
            _BucketSpec(
                key=(category, "tenant", tenant_id, ""),
                capacity=policy.tenant_capacity,
                refill_per_second=policy.tenant_refill_per_second,
            )
        )
    if policy.user_capacity > 0:
        specs.append(
            _BucketSpec(
                key=(category, "user", tenant_id, user_id),
                capacity=policy.user_capacity,
                refill_per_second=policy.user_refill_per_second,
            )
        )
    return tuple(specs)


def _refilled_bucket(
    bucket: _Bucket | None,
    capacity: int,
    refill_per_second: float,
    now: float,
) -> _Bucket:
    if bucket is None:
        return _Bucket(tokens=float(capacity), updated_at=now)
    elapsed = max(0.0, now - bucket.updated_at)
    return _Bucket(
        tokens=min(float(capacity), bucket.tokens + elapsed * refill_per_second),
        updated_at=max(now, bucket.updated_at),
    )


def _rejected_decision(
    specs: tuple[_BucketSpec, ...],
    states: list[_Bucket],
) -> RateLimitDecision | None:
    rejected = [
        (spec, state) for spec, state in zip(specs, states, strict=True) if state.tokens < 1.0
    ]
    if not rejected:
        return None
    retry_after = min(
        MAX_RETRY_AFTER_SECONDS,
        max(
            1,
            math.ceil(
                max((1.0 - state.tokens) / spec.refill_per_second for spec, state in rejected)
            ),
        ),
    )
    return RateLimitDecision(
        allowed=False,
        retry_after_seconds=retry_after,
        rejected_scope=rejected[0][0].key[1],
    )


def _policy_from_env(
    env: Mapping[str, str],
    *,
    tenant_capacity_env: str,
    tenant_refill_env: str,
    user_capacity_env: str,
    user_refill_env: str,
) -> RateLimitPolicy:
    return RateLimitPolicy(
        tenant_capacity=_non_negative_int_env(env, tenant_capacity_env, 0),
        tenant_refill_per_second=_positive_float_env(
            env, tenant_refill_env, DEFAULT_RATE_LIMIT_REFILL_PER_SECOND
        ),
        user_capacity=_non_negative_int_env(env, user_capacity_env, 0),
        user_refill_per_second=_positive_float_env(
            env, user_refill_env, DEFAULT_RATE_LIMIT_REFILL_PER_SECOND
        ),
    )


def _non_negative_int_env(
    env: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    configured = env.get(name, "").strip()
    if not configured:
        return default
    try:
        value = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be a non-negative integer")
    return value


def _positive_float_env(
    env: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    configured = env.get(name, "").strip()
    if not configured:
        return default
    try:
        value = float(configured)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive finite number")
    return value
