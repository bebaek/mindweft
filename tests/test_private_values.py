from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.private_values import InMemoryPrivateValueStore, LocalPIIProtector


def test_private_value_store_is_scoped_by_tenant_user_and_thread() -> None:
    store = InMemoryPrivateValueStore()
    store.add(
        "tenant-a",
        "thread-a",
        {"ref": "private value"},
        user_id="user-a",
    )
    placeholder = "{{pii:name:ref}}"

    assert (
        store.render_for_user("tenant-a", "thread-a", placeholder, user_id="user-a")
        == "private value"
    )
    assert (
        store.render_for_user("tenant-a", "thread-a", placeholder, user_id="user-b") == placeholder
    )
    assert (
        store.render_for_user("tenant-b", "thread-a", placeholder, user_id="user-a") == placeholder
    )
    assert (
        store.render_for_user("tenant-a", "thread-b", placeholder, user_id="user-a") == placeholder
    )


def test_private_value_store_binds_references_to_declared_kinds() -> None:
    store = InMemoryPrivateValueStore()
    store.add(
        "tenant",
        "thread",
        {"ref": "private@example.com"},
        kinds={"ref": "email"},
    )

    assert store.render_for_user("tenant", "thread", "{{pii:phone:ref}}") == "{{pii:phone:ref}}"
    with pytest.raises(HTTPException, match="kind does not match"):
        store.resolve_for_tool("tenant", "thread", "{{pii:phone:ref}}")
    with pytest.raises(HTTPException, match="kind collision"):
        store.add(
            "tenant",
            "thread",
            {"ref": "private@example.com"},
            kinds={"ref": "phone"},
        )

    # An update without kind metadata must not downgrade an existing binding.
    store.add("tenant", "thread", {"ref": "private@example.com"})
    with pytest.raises(HTTPException, match="kind does not match"):
        store.resolve_for_tool("tenant", "thread", "{{pii:phone:ref}}")


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


def test_private_value_store_resolves_for_trusted_tool_strictly() -> None:
    store = InMemoryPrivateValueStore()
    store.add("tenant", "thread", {"email-ref": "private@example.com"})

    assert (
        store.resolve_for_tool(
            "tenant",
            "thread",
            "Send to {{pii:email:email-ref}}",
        )
        == "Send to private@example.com"
    )
    with pytest.raises(HTTPException, match="missing or expired"):
        store.resolve_for_tool(
            "tenant",
            "thread",
            "Send to {{pii:email:missing-ref}}",
        )


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


def test_local_pii_protector_masks_common_input_pii() -> None:
    references = iter(["email-ref", "address-ref", "phone-ref", "person-ref"])
    protector = LocalPIIProtector(reference_factory=lambda: next(references))

    result = protector.protect(
        "Email Jane Doe at jane@example.com, call +1 (415) 555-0123, "
        "or visit 123 Main Street, Apt 4, Springfield, IL 62704."
    )

    assert "Jane Doe" not in result.text
    assert "jane@example.com" not in result.text
    assert "+1 (415) 555-0123" not in result.text
    assert "123 Main Street, Apt 4, Springfield, IL 62704" not in result.text
    assert set(result.private_values.values()) == {
        "Jane Doe",
        "jane@example.com",
        "+1 (415) 555-0123",
        "123 Main Street, Apt 4, Springfield, IL 62704",
    }
    assert set(result.private_value_kinds.values()) == {
        "person",
        "email",
        "phone",
        "address",
    }
    assert "{{pii:person:" in result.text
    assert "{{pii:email:" in result.text
    assert "{{pii:phone:" in result.text
    assert "{{pii:address:" in result.text


def test_local_pii_protector_preserves_existing_placeholders() -> None:
    protector = LocalPIIProtector(reference_factory=lambda: "new-ref")

    result = protector.protect("Email {{pii:contact:known-ref}} at private@example.com")

    assert "{{pii:contact:known-ref}}" in result.text
    assert result.private_values == {"new-ref": "private@example.com"}


def test_local_pii_protector_can_be_disabled() -> None:
    protector = LocalPIIProtector.from_env({"MINIGENT_INPUT_PII_PROTECTION_ENABLED": "false"})

    result = protector.protect("Email jane@example.com")

    assert result.text == "Email jane@example.com"
    assert result.private_values == {}


def test_local_pii_protector_rejects_invalid_boolean_setting() -> None:
    with pytest.raises(RuntimeError, match="must be true or false"):
        LocalPIIProtector.from_env({"MINIGENT_INPUT_PII_PROTECTION_ENABLED": "sometimes"})
