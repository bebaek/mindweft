from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.rate_limits import (
    InMemoryRateLimiter,
    RateLimitPolicy,
    RateLimitSettings,
    RunConcurrencyPolicy,
    SQLiteRateLimiter,
)


def test_rate_limit_settings_default_disabled_and_parse_values() -> None:
    defaults = RateLimitSettings.from_env({})
    assert defaults.db_path is None
    assert defaults.uploads.enabled is False
    assert defaults.runs.enabled is False

    settings = RateLimitSettings.from_env(
        {
            "MINIGENT_RATE_LIMIT_DB_PATH": "/data/rate-limits.db",
            "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_CAPACITY": "30",
            "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_REFILL_PER_SECOND": "0.5",
            "MINIGENT_UPLOAD_RATE_LIMIT_USER_CAPACITY": "10",
            "MINIGENT_UPLOAD_RATE_LIMIT_USER_REFILL_PER_SECOND": "0.2",
            "MINIGENT_RUN_RATE_LIMIT_TENANT_CAPACITY": "15",
            "MINIGENT_RUN_RATE_LIMIT_TENANT_REFILL_PER_SECOND": "0.3",
            "MINIGENT_RUN_RATE_LIMIT_USER_CAPACITY": "5",
            "MINIGENT_RUN_RATE_LIMIT_USER_REFILL_PER_SECOND": "0.1",
            "MINIGENT_RUN_CONCURRENCY_TENANT_CAPACITY": "4",
            "MINIGENT_RUN_CONCURRENCY_USER_CAPACITY": "2",
            "MINIGENT_RUN_CONCURRENCY_LEASE_SECONDS": "60",
            "MINIGENT_RUN_CONCURRENCY_HEARTBEAT_SECONDS": "20",
        }
    )

    assert settings.db_path == "/data/rate-limits.db"
    assert settings.uploads == RateLimitPolicy(
        tenant_capacity=30,
        tenant_refill_per_second=0.5,
        user_capacity=10,
        user_refill_per_second=0.2,
    )
    assert settings.runs == RateLimitPolicy(
        tenant_capacity=15,
        tenant_refill_per_second=0.3,
        user_capacity=5,
        user_refill_per_second=0.1,
    )
    assert settings.concurrent_runs == RunConcurrencyPolicy(
        tenant_capacity=4,
        user_capacity=2,
        lease_seconds=60,
        heartbeat_seconds=20,
    )

    with pytest.raises(RuntimeError, match="non-negative integer"):
        RateLimitSettings.from_env({"MINIGENT_RUN_RATE_LIMIT_USER_CAPACITY": "-1"})
    with pytest.raises(RuntimeError, match="positive finite number"):
        RateLimitSettings.from_env({"MINIGENT_RUN_RATE_LIMIT_USER_REFILL_PER_SECOND": "nan"})

    with pytest.raises(RuntimeError, match="must be less than"):
        RateLimitSettings.from_env(
            {
                "MINIGENT_RUN_CONCURRENCY_TENANT_CAPACITY": "1",
                "MINIGENT_RUN_CONCURRENCY_LEASE_SECONDS": "20",
                "MINIGENT_RUN_CONCURRENCY_HEARTBEAT_SECONDS": "20",
            }
        )


def test_run_concurrency_leases_enforce_scopes_renew_release_and_expiry(
    tmp_path: Path,
) -> None:
    limiters = [InMemoryRateLimiter(), SQLiteRateLimiter(tmp_path / "concurrency.db")]
    policy = RunConcurrencyPolicy(
        tenant_capacity=2,
        user_capacity=1,
        lease_seconds=60,
        heartbeat_seconds=20,
    )
    for limiter in limiters:
        first = limiter.acquire_run_slot("tenant-1", "user-1", policy, now=100)
        assert first.allowed and first.lease is not None
        user_rejected = limiter.acquire_run_slot("tenant-1", "user-1", policy, now=100)
        assert user_rejected.allowed is False
        assert user_rejected.rejected_scope == "user"
        assert user_rejected.retry_after_seconds == 60

        second = limiter.acquire_run_slot("tenant-1", "user-2", policy, now=100)
        assert second.allowed and second.lease is not None
        tenant_rejected = limiter.acquire_run_slot("tenant-1", "user-3", policy, now=100)
        assert tenant_rejected.allowed is False
        assert tenant_rejected.rejected_scope == "tenant"
        assert limiter.acquire_run_slot("tenant-2", "user-1", policy, now=100).allowed

        statistics = limiter.run_concurrency_statistics("tenant-1", now=100)
        assert statistics.active_runs == 2
        assert statistics.active_users == 2
        assert statistics.next_expiration == 160
        assert limiter.renew_run_slot(first.lease, policy, now=130) is True
        assert limiter.release_run_slot(second.lease) is True
        assert limiter.run_concurrency_statistics("tenant-1", now=161).active_runs == 1
        assert limiter.run_concurrency_statistics("tenant-1", now=191).active_runs == 0
        assert limiter.renew_run_slot(first.lease, policy, now=191) is False


def test_sqlite_run_concurrency_acquisition_is_atomic_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-runs.db"
    limiters = [SQLiteRateLimiter(path), SQLiteRateLimiter(path)]
    policy = RunConcurrencyPolicy(
        tenant_capacity=1,
        user_capacity=1,
        lease_seconds=60,
        heartbeat_seconds=20,
    )
    barrier = Barrier(2)

    def acquire(limiter: SQLiteRateLimiter) -> bool:
        barrier.wait()
        return limiter.acquire_run_slot("tenant-1", "user-1", policy, now=100).allowed

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(acquire, limiters))

    assert sorted(decisions) == [False, True]


def test_rate_limiter_isolates_user_and_tenant_buckets() -> None:
    limiter = InMemoryRateLimiter()
    user_policy = RateLimitPolicy(
        tenant_capacity=10,
        tenant_refill_per_second=1,
        user_capacity=1,
        user_refill_per_second=0.1,
    )

    assert limiter.consume("upload", "tenant-1", "user-1", user_policy, now=0).allowed
    rejected_user = limiter.consume("upload", "tenant-1", "user-1", user_policy, now=0)
    assert rejected_user.allowed is False
    assert rejected_user.rejected_scope == "user"
    assert rejected_user.retry_after_seconds == 10
    assert limiter.consume("upload", "tenant-1", "user-2", user_policy, now=0).allowed

    tenant_policy = RateLimitPolicy(
        tenant_capacity=1,
        tenant_refill_per_second=0.25,
        user_capacity=10,
        user_refill_per_second=1,
    )
    assert limiter.consume("run", "tenant-1", "user-1", tenant_policy, now=0).allowed
    rejected_tenant = limiter.consume("run", "tenant-1", "user-2", tenant_policy, now=0)
    assert rejected_tenant.allowed is False
    assert rejected_tenant.rejected_scope == "tenant"
    assert rejected_tenant.retry_after_seconds == 4
    assert limiter.consume("run", "tenant-2", "user-1", tenant_policy, now=0).allowed

    bounded_policy = RateLimitPolicy(tenant_capacity=1, tenant_refill_per_second=1e-12)
    assert limiter.consume("bounded", "tenant-1", "user-1", bounded_policy, now=0).allowed
    bounded = limiter.consume("bounded", "tenant-1", "user-1", bounded_policy, now=0)
    assert bounded.retry_after_seconds == 86_400


def test_rate_limiter_refills_and_separates_categories() -> None:
    limiter = InMemoryRateLimiter()
    policy = RateLimitPolicy(tenant_capacity=1, tenant_refill_per_second=0.5)

    assert limiter.consume("upload", "tenant-1", "user-1", policy, now=10).allowed
    rejected = limiter.consume("upload", "tenant-1", "user-1", policy, now=11)
    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 1
    assert limiter.consume("upload", "tenant-1", "user-1", policy, now=12).allowed
    assert limiter.consume("run", "tenant-1", "user-1", policy, now=12).allowed


def test_sqlite_rate_limit_is_atomic_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.db"
    limiters = [SQLiteRateLimiter(path), SQLiteRateLimiter(path)]
    policy = RateLimitPolicy(tenant_capacity=1, tenant_refill_per_second=0.1)
    barrier = Barrier(2)

    def consume(limiter: SQLiteRateLimiter) -> bool:
        barrier.wait()
        return limiter.consume("run", "tenant-1", "user-1", policy, now=100).allowed

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(consume, limiters))

    assert sorted(decisions) == [False, True]
    retry = limiters[0].consume("run", "tenant-1", "user-1", policy, now=100)
    assert retry.allowed is False
    assert retry.retry_after_seconds == 10
