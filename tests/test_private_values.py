from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.private_values import InMemoryPrivateValueStore


def test_private_value_store_is_scoped_by_tenant_and_thread() -> None:
    store = InMemoryPrivateValueStore()
    store.add("tenant-a", "thread-a", {"ref": "private value"})
    placeholder = "{{pii:name:ref}}"

    assert store.render_for_user("tenant-a", "thread-a", placeholder) == "private value"
    assert store.render_for_user("tenant-b", "thread-a", placeholder) == placeholder
    assert store.render_for_user("tenant-a", "thread-b", placeholder) == placeholder


def test_private_value_store_expires_values() -> None:
    now = [100.0]
    store = InMemoryPrivateValueStore(ttl_seconds=5, clock=lambda: now[0])
    store.add("tenant", "thread", {"ref": "private value"})
    now[0] = 106.0

    assert store.render_for_user("tenant", "thread", "{{pii:name:ref}}") == "{{pii:name:ref}}"


def test_private_value_store_enforces_reference_and_size_limits() -> None:
    store = InMemoryPrivateValueStore(max_refs_per_thread=1, max_value_chars=5)
    store.add("tenant", "thread", {"first": "value"})

    with pytest.raises(HTTPException, match="reference limit"):
        store.add("tenant", "thread", {"second": "value"})
    with pytest.raises(HTTPException, match="character limit"):
        store.add("tenant", "other-thread", {"large": "too long"})


def test_private_value_store_clear_thread_removes_values() -> None:
    store = InMemoryPrivateValueStore()
    store.add("tenant", "thread", {"ref": "private value"})

    store.clear_thread("tenant", "thread")

    assert store.render_for_user("tenant", "thread", "{{pii:name:ref}}") == "{{pii:name:ref}}"


def test_private_value_store_reads_limits_from_environment_mapping() -> None:
    store = InMemoryPrivateValueStore.from_env(
        {
            "MINIGENT_PRIVATE_VALUE_TTL_SECONDS": "5",
            "MINIGENT_PRIVATE_VALUE_MAX_REFS_PER_THREAD": "1",
            "MINIGENT_PRIVATE_VALUE_MAX_CHARS": "5",
        }
    )
    store.add("tenant", "thread", {"ref": "value"})

    with pytest.raises(HTTPException, match="reference limit"):
        store.add("tenant", "thread", {"other": "value"})
