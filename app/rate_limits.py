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
from uuid import uuid4

RATE_LIMIT_DB_PATH_ENV = "MINIGENT_RATE_LIMIT_DB_PATH"
UPLOAD_TENANT_CAPACITY_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_CAPACITY"
UPLOAD_TENANT_REFILL_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_REFILL_PER_SECOND"
UPLOAD_USER_CAPACITY_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_USER_CAPACITY"
UPLOAD_USER_REFILL_ENV = "MINIGENT_UPLOAD_RATE_LIMIT_USER_REFILL_PER_SECOND"
RUN_TENANT_CAPACITY_ENV = "MINIGENT_RUN_RATE_LIMIT_TENANT_CAPACITY"
RUN_TENANT_REFILL_ENV = "MINIGENT_RUN_RATE_LIMIT_TENANT_REFILL_PER_SECOND"
RUN_USER_CAPACITY_ENV = "MINIGENT_RUN_RATE_LIMIT_USER_CAPACITY"
RUN_USER_REFILL_ENV = "MINIGENT_RUN_RATE_LIMIT_USER_REFILL_PER_SECOND"
RUN_CONCURRENCY_TENANT_CAPACITY_ENV = "MINIGENT_RUN_CONCURRENCY_TENANT_CAPACITY"
RUN_CONCURRENCY_USER_CAPACITY_ENV = "MINIGENT_RUN_CONCURRENCY_USER_CAPACITY"
RUN_CONCURRENCY_LEASE_SECONDS_ENV = "MINIGENT_RUN_CONCURRENCY_LEASE_SECONDS"
RUN_CONCURRENCY_HEARTBEAT_SECONDS_ENV = "MINIGENT_RUN_CONCURRENCY_HEARTBEAT_SECONDS"
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 1.0 / 60.0
DEFAULT_RUN_CONCURRENCY_LEASE_SECONDS = 60
DEFAULT_RUN_CONCURRENCY_HEARTBEAT_SECONDS = 20
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
class RunConcurrencyPolicy:
    tenant_capacity: int = 0
    user_capacity: int = 0
    lease_seconds: int = DEFAULT_RUN_CONCURRENCY_LEASE_SECONDS
    heartbeat_seconds: int = DEFAULT_RUN_CONCURRENCY_HEARTBEAT_SECONDS

    @property
    def enabled(self) -> bool:
        return self.tenant_capacity > 0 or self.user_capacity > 0


@dataclass(frozen=True)
class RateLimitSettings:
    db_path: str | None = None
    uploads: RateLimitPolicy = RateLimitPolicy()
    runs: RateLimitPolicy = RateLimitPolicy()
    concurrent_runs: RunConcurrencyPolicy = RunConcurrencyPolicy()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RateLimitSettings:
        lookup = os.environ if env is None else env
        settings = cls(
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
            concurrent_runs=RunConcurrencyPolicy(
                tenant_capacity=_non_negative_int_env(
                    lookup, RUN_CONCURRENCY_TENANT_CAPACITY_ENV, 0
                ),
                user_capacity=_non_negative_int_env(lookup, RUN_CONCURRENCY_USER_CAPACITY_ENV, 0),
                lease_seconds=_positive_int_env(
                    lookup,
                    RUN_CONCURRENCY_LEASE_SECONDS_ENV,
                    DEFAULT_RUN_CONCURRENCY_LEASE_SECONDS,
                ),
                heartbeat_seconds=_positive_int_env(
                    lookup,
                    RUN_CONCURRENCY_HEARTBEAT_SECONDS_ENV,
                    DEFAULT_RUN_CONCURRENCY_HEARTBEAT_SECONDS,
                ),
            ),
        )
        concurrency = settings.concurrent_runs
        if concurrency.enabled and concurrency.heartbeat_seconds >= concurrency.lease_seconds:
            raise RuntimeError(
                f"{RUN_CONCURRENCY_HEARTBEAT_SECONDS_ENV} must be less than "
                f"{RUN_CONCURRENCY_LEASE_SECONDS_ENV}"
            )
        return settings


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    rejected_scope: str | None = None


@dataclass(frozen=True)
class RunConcurrencyLease:
    lease_id: str | None
    tenant_id: str
    user_id: str
    expires_at: float


@dataclass(frozen=True)
class RunConcurrencyDecision:
    allowed: bool
    lease: RunConcurrencyLease | None = None
    retry_after_seconds: int = 0
    rejected_scope: str | None = None


@dataclass(frozen=True)
class RunConcurrencyStatistics:
    active_runs: int
    active_users: int
    next_expiration: float | None = None


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

    def acquire_run_slot(
        self,
        tenant_id: str,
        user_id: str,
        policy: RunConcurrencyPolicy,
        *,
        now: float | None = None,
    ) -> RunConcurrencyDecision: ...

    def renew_run_slot(
        self,
        lease: RunConcurrencyLease,
        policy: RunConcurrencyPolicy,
        *,
        now: float | None = None,
    ) -> bool: ...

    def release_run_slot(self, lease: RunConcurrencyLease) -> bool: ...

    def run_concurrency_statistics(
        self, tenant_id: str, *, now: float | None = None
    ) -> RunConcurrencyStatistics: ...


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass(frozen=True)
class _BucketSpec:
    key: tuple[str, str, str, str]
    capacity: int
    refill_per_second: float


@dataclass
class _RunLeaseRecord:
    lease_id: str
    tenant_id: str
    user_id: str
    expires_at: float


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, str, str], _Bucket] = {}
        self._run_leases: dict[str, _RunLeaseRecord] = {}
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

    def acquire_run_slot(
        self,
        tenant_id: str,
        user_id: str,
        policy: RunConcurrencyPolicy,
        *,
        now: float | None = None,
    ) -> RunConcurrencyDecision:
        timestamp = time.time() if now is None else now
        if not policy.enabled:
            return RunConcurrencyDecision(
                allowed=True,
                lease=RunConcurrencyLease(None, tenant_id, user_id, timestamp),
            )
        with self._lock:
            self._purge_expired_run_leases(timestamp)
            records = list(self._run_leases.values())
            rejected = _concurrency_rejection(records, tenant_id, user_id, policy, timestamp)
            if rejected is not None:
                return rejected
            lease_id = str(uuid4())
            expires_at = timestamp + policy.lease_seconds
            self._run_leases[lease_id] = _RunLeaseRecord(
                lease_id=lease_id,
                tenant_id=tenant_id,
                user_id=user_id,
                expires_at=expires_at,
            )
        return RunConcurrencyDecision(
            allowed=True,
            lease=RunConcurrencyLease(lease_id, tenant_id, user_id, expires_at),
        )

    def renew_run_slot(
        self,
        lease: RunConcurrencyLease,
        policy: RunConcurrencyPolicy,
        *,
        now: float | None = None,
    ) -> bool:
        if lease.lease_id is None:
            return True
        timestamp = time.time() if now is None else now
        with self._lock:
            record = self._run_leases.get(lease.lease_id)
            if record is None or record.expires_at <= timestamp:
                self._run_leases.pop(lease.lease_id, None)
                return False
            record.expires_at = timestamp + policy.lease_seconds
            return True

    def release_run_slot(self, lease: RunConcurrencyLease) -> bool:
        if lease.lease_id is None:
            return True
        with self._lock:
            return self._run_leases.pop(lease.lease_id, None) is not None

    def run_concurrency_statistics(
        self, tenant_id: str, *, now: float | None = None
    ) -> RunConcurrencyStatistics:
        timestamp = time.time() if now is None else now
        with self._lock:
            self._purge_expired_run_leases(timestamp)
            records = [
                record for record in self._run_leases.values() if record.tenant_id == tenant_id
            ]
        return _run_concurrency_statistics(records)

    def _purge_expired_run_leases(self, now: float) -> None:
        expired = [
            lease_id for lease_id, record in self._run_leases.items() if record.expires_at <= now
        ]
        for lease_id in expired:
            del self._run_leases[lease_id]


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS concurrent_run_leases (
                    lease_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_concurrent_run_leases_tenant "
                "ON concurrent_run_leases (tenant_id, expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_concurrent_run_leases_user "
                "ON concurrent_run_leases (tenant_id, user_id, expires_at)"
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

    def acquire_run_slot(
        self,
        tenant_id: str,
        user_id: str,
        policy: RunConcurrencyPolicy,
        *,
        now: float | None = None,
    ) -> RunConcurrencyDecision:
        timestamp = time.time() if now is None else now
        if not policy.enabled:
            return RunConcurrencyDecision(
                allowed=True,
                lease=RunConcurrencyLease(None, tenant_id, user_id, timestamp),
            )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM concurrent_run_leases WHERE expires_at <= ?",
                (timestamp,),
            )
            records = [
                _RunLeaseRecord(str(row[0]), str(row[1]), str(row[2]), float(row[3]))
                for row in connection.execute(
                    """
                    SELECT lease_id, tenant_id, user_id, expires_at
                    FROM concurrent_run_leases
                    WHERE tenant_id = ?
                    """,
                    (tenant_id,),
                ).fetchall()
            ]
            rejected = _concurrency_rejection(records, tenant_id, user_id, policy, timestamp)
            if rejected is not None:
                return rejected
            lease_id = str(uuid4())
            expires_at = timestamp + policy.lease_seconds
            connection.execute(
                """
                INSERT INTO concurrent_run_leases (lease_id, tenant_id, user_id, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (lease_id, tenant_id, user_id, expires_at),
            )
        return RunConcurrencyDecision(
            allowed=True,
            lease=RunConcurrencyLease(lease_id, tenant_id, user_id, expires_at),
        )

    def renew_run_slot(
        self,
        lease: RunConcurrencyLease,
        policy: RunConcurrencyPolicy,
        *,
        now: float | None = None,
    ) -> bool:
        if lease.lease_id is None:
            return True
        timestamp = time.time() if now is None else now
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE concurrent_run_leases
                SET expires_at = ?
                WHERE lease_id = ? AND tenant_id = ? AND user_id = ? AND expires_at > ?
                """,
                (
                    timestamp + policy.lease_seconds,
                    lease.lease_id,
                    lease.tenant_id,
                    lease.user_id,
                    timestamp,
                ),
            )
            return cursor.rowcount > 0

    def release_run_slot(self, lease: RunConcurrencyLease) -> bool:
        if lease.lease_id is None:
            return True
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM concurrent_run_leases
                WHERE lease_id = ? AND tenant_id = ? AND user_id = ?
                """,
                (lease.lease_id, lease.tenant_id, lease.user_id),
            )
            return cursor.rowcount > 0

    def run_concurrency_statistics(
        self, tenant_id: str, *, now: float | None = None
    ) -> RunConcurrencyStatistics:
        timestamp = time.time() if now is None else now
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM concurrent_run_leases WHERE expires_at <= ?",
                (timestamp,),
            )
            row = connection.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT user_id), MIN(expires_at)
                FROM concurrent_run_leases
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
        return RunConcurrencyStatistics(
            active_runs=int(row[0]),
            active_users=int(row[1]),
            next_expiration=float(row[2]) if row[2] is not None else None,
        )


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


def _concurrency_rejection(
    records: list[_RunLeaseRecord],
    tenant_id: str,
    user_id: str,
    policy: RunConcurrencyPolicy,
    now: float,
) -> RunConcurrencyDecision | None:
    tenant_records = [record for record in records if record.tenant_id == tenant_id]
    user_records = [record for record in tenant_records if record.user_id == user_id]
    rejected_scope: str | None = None
    blocking_records: list[_RunLeaseRecord] = []
    if policy.tenant_capacity > 0 and len(tenant_records) >= policy.tenant_capacity:
        rejected_scope = "tenant"
        blocking_records = tenant_records
    elif policy.user_capacity > 0 and len(user_records) >= policy.user_capacity:
        rejected_scope = "user"
        blocking_records = user_records
    if rejected_scope is None:
        return None
    retry_after = min(
        MAX_RETRY_AFTER_SECONDS,
        max(1, math.ceil(min(record.expires_at for record in blocking_records) - now)),
    )
    return RunConcurrencyDecision(
        allowed=False,
        retry_after_seconds=retry_after,
        rejected_scope=rejected_scope,
    )


def _run_concurrency_statistics(
    records: list[_RunLeaseRecord],
) -> RunConcurrencyStatistics:
    return RunConcurrencyStatistics(
        active_runs=len(records),
        active_users=len({record.user_id for record in records}),
        next_expiration=min((record.expires_at for record in records), default=None),
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


def _positive_int_env(
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
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
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
