from __future__ import annotations

import asyncio
from pathlib import Path

from app.admin_store import SQLiteTenantConfigStore
from app.external_grants import (
    ExternalGrant,
    ExternalGrantProviderError,
    ExternalGrantProviderRegistry,
)
from app.models import Tenant, TenantUser, TenantUserRole, TenantUserStatus
from app.user_deprovisioning import (
    UserDeprovisioningProcessor,
    UserDeprovisioningSettings,
)


def _store_with_user(path: Path) -> SQLiteTenantConfigStore:
    store = SQLiteTenantConfigStore(str(path))
    store.create_tenant(Tenant(id="tenant-1", slug="tenant-one", name="Tenant One"))
    store.create_tenant_user(
        TenantUser(
            id="record-1",
            tenant_id="tenant-1",
            user_id="user-1",
            role=TenantUserRole.MEMBER,
            status=TenantUserStatus.ACTIVE,
        )
    )
    store.upsert_subject_mcp_server_catalog_assignment(
        "tenant-1",
        "user",
        "user-1",
        item_ids=["dav"],
        updated_by="admin-user",
    )
    return store


def test_inactive_transition_atomically_enqueues_durable_deprovisioning(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    store = _store_with_user(path)

    updated = store.update_tenant_user(
        "tenant-1",
        "record-1",
        status=TenantUserStatus.SUSPENDED,
        updated_by="admin-user",
    )

    assert updated is not None
    events, total = SQLiteTenantConfigStore(str(path)).list_user_deprovisioning_events("tenant-1")
    assert total == 1
    assert events[0].user_id == "user-1"
    assert events[0].target_status == TenantUserStatus.SUSPENDED
    assert events[0].actor_user_id == "admin-user"
    assert events[0].state == "pending"

    store.update_tenant_user(
        "tenant-1",
        "record-1",
        display_name="Suspended User",
        updated_by="admin-user",
    )
    assert store.list_user_deprovisioning_events("tenant-1")[1] == 1


def test_processor_disables_grants_and_removes_assignment_idempotently(tmp_path: Path) -> None:
    store = _store_with_user(tmp_path / "admin.db")
    store.update_tenant_user(
        "tenant-1",
        "record-1",
        status=TenantUserStatus.DELETED,
        updated_by="admin-user",
    )
    grants = [
        ExternalGrant(
            resource_id="calendar:one",
            subject_id="user-1",
            permission="read_write",
            enabled=True,
        ),
        ExternalGrant(
            resource_id="calendar:other",
            subject_id="other-user",
            permission="read",
            enabled=True,
        ),
    ]

    class FakeProvider:
        provider_id = "dav"

        async def list_grants(self, *, tenant_id: str, actor_user_id: str):
            assert tenant_id == "tenant-1"
            assert actor_user_id == "admin-user"
            return list(grants)

        async def upsert_grant(
            self,
            *,
            tenant_id: str,
            actor_user_id: str,
            resource_id: str,
            subject_id: str,
            permission: str,
            enabled: bool,
        ):
            replacement = ExternalGrant(
                resource_id=resource_id,
                subject_id=subject_id,
                permission=permission,
                enabled=enabled,
                updated_by=actor_user_id,
            )
            grants[:] = [
                replacement
                if (grant.resource_id, grant.subject_id) == (resource_id, subject_id)
                else grant
                for grant in grants
            ]
            return replacement

    processor = UserDeprovisioningProcessor(
        store,
        ExternalGrantProviderRegistry([FakeProvider()]),  # type: ignore[list-item]
    )
    event = asyncio.run(processor.process_one())

    assert event is not None
    assert event.state == "completed"
    assert event.assignment_removed is True
    assert event.grants_disabled == 1
    assert store.get_subject_mcp_server_catalog_assignment("tenant-1", "user", "user-1") is None
    assert grants[0].enabled is False
    assert grants[1].enabled is True
    assert asyncio.run(processor.process_one()) is None


def test_processor_retries_then_dead_letters_provider_failures(tmp_path: Path) -> None:
    store = _store_with_user(tmp_path / "admin.db")
    store.update_tenant_user(
        "tenant-1",
        "record-1",
        status=TenantUserStatus.SUSPENDED,
        updated_by="admin-user",
    )

    class FailingProvider:
        provider_id = "dav"

        async def list_grants(self, *, tenant_id: str, actor_user_id: str):
            _ = tenant_id, actor_user_id
            raise ExternalGrantProviderError(503, "provider unavailable")

    processor = UserDeprovisioningProcessor(
        store,
        ExternalGrantProviderRegistry([FailingProvider()]),  # type: ignore[list-item]
        settings=UserDeprovisioningSettings(max_attempts=1),
    )
    event = asyncio.run(processor.process_one())

    assert event is not None
    assert event.state == "dead_letter"
    assert event.attempts == 1
    assert event.assignment_removed is True
    assert event.last_error == "dav: provider unavailable"
    assert store.retry_user_deprovisioning_event("tenant-1", event.id) is True
    retried = store.get_user_deprovisioning_event("tenant-1", event.id)
    assert retried is not None
    assert retried.state == "pending"


def test_claiming_is_cross_instance_atomic(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    first = _store_with_user(path)
    first.update_tenant_user(
        "tenant-1",
        "record-1",
        status=TenantUserStatus.SUSPENDED,
        updated_by="admin-user",
    )
    second = SQLiteTenantConfigStore(str(path))

    claimed = first.claim_user_deprovisioning_event()

    assert claimed is not None
    assert second.claim_user_deprovisioning_event() is None
