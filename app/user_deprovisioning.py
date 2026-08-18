from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.admin_store import SQLiteTenantConfigStore, UserDeprovisioningEvent
from app.external_grants import ExternalGrantProviderError, ExternalGrantProviderRegistry
from minigent_config.unified_config import preferred_mindweft_env

logger = logging.getLogger(__name__)

DEPROVISIONING_INTERVAL_ENV = "MINIGENT_USER_DEPROVISIONING_INTERVAL_SECONDS"
DEPROVISIONING_MAX_ATTEMPTS_ENV = "MINIGENT_USER_DEPROVISIONING_MAX_ATTEMPTS"
DEPROVISIONING_LEASE_ENV = "MINIGENT_USER_DEPROVISIONING_LEASE_SECONDS"


@dataclass(frozen=True)
class UserDeprovisioningSettings:
    interval_seconds: float = 5.0
    max_attempts: int = 8
    lease_seconds: int = 60

    @classmethod
    def from_env(cls) -> UserDeprovisioningSettings:
        return cls(
            interval_seconds=_positive_float_env(DEPROVISIONING_INTERVAL_ENV, 5.0),
            max_attempts=_positive_int_env(DEPROVISIONING_MAX_ATTEMPTS_ENV, 8),
            lease_seconds=_positive_int_env(DEPROVISIONING_LEASE_ENV, 60),
        )


class UserDeprovisioningProcessor:
    def __init__(
        self,
        store: SQLiteTenantConfigStore,
        providers: ExternalGrantProviderRegistry,
        *,
        settings: UserDeprovisioningSettings | None = None,
    ) -> None:
        self._store = store
        self._providers = providers
        self.settings = settings or UserDeprovisioningSettings.from_env()

    async def process_one(self) -> UserDeprovisioningEvent | None:
        event = await asyncio.to_thread(
            self._store.claim_user_deprovisioning_event,
            lease_seconds=self.settings.lease_seconds,
        )
        if event is None:
            return None

        assignment_removed = event.assignment_removed
        grants_disabled = event.grants_disabled
        errors: list[str] = []
        try:
            removed = await asyncio.to_thread(
                self._store.delete_subject_mcp_server_catalog_assignment,
                event.tenant_id,
                "user",
                event.user_id,
            )
            assignment_removed = assignment_removed or removed
        except Exception as exc:  # pragma: no cover - defensive storage boundary
            logger.exception("user_deprovisioning.assignment_cleanup_failed event_id=%s", event.id)
            errors.append(f"catalog assignment cleanup failed: {type(exc).__name__}")

        for provider in self._providers.list():
            try:
                await asyncio.to_thread(
                    self._store.heartbeat_user_deprovisioning_event,
                    event.id,
                )
                grants = await provider.list_grants(
                    tenant_id=event.tenant_id,
                    actor_user_id=event.actor_user_id,
                )
                for grant in grants:
                    if grant.subject_id != event.user_id or not grant.enabled:
                        continue
                    await asyncio.to_thread(
                        self._store.heartbeat_user_deprovisioning_event,
                        event.id,
                    )
                    await provider.upsert_grant(
                        tenant_id=event.tenant_id,
                        actor_user_id=event.actor_user_id,
                        resource_id=grant.resource_id,
                        subject_id=grant.subject_id,
                        permission=grant.permission,
                        enabled=False,
                    )
                    grants_disabled += 1
            except ExternalGrantProviderError as exc:
                errors.append(f"{provider.provider_id}: {exc.detail}")
            except Exception as exc:  # pragma: no cover - defensive provider boundary
                logger.exception(
                    "user_deprovisioning.provider_failed event_id=%s provider_id=%s",
                    event.id,
                    provider.provider_id,
                )
                errors.append(f"{provider.provider_id}: {type(exc).__name__}")

        if not errors:
            await asyncio.to_thread(
                self._store.complete_user_deprovisioning_event,
                event.id,
                assignment_removed=assignment_removed,
                grants_disabled=grants_disabled,
            )
        else:
            dead_letter = event.attempts >= self.settings.max_attempts
            retry_delay = min(3600, 2 ** min(event.attempts, 11))
            await asyncio.to_thread(
                self._store.fail_user_deprovisioning_event,
                event.id,
                error="; ".join(errors),
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=retry_delay),
                dead_letter=dead_letter,
                assignment_removed=assignment_removed,
                grants_disabled=grants_disabled,
            )
        return self._store.get_user_deprovisioning_event(event.tenant_id, event.id)

    async def run(self) -> None:
        while True:
            try:
                while await self.process_one() is not None:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive background boundary
                logger.exception("user_deprovisioning.worker_failed")
            await asyncio.sleep(self.settings.interval_seconds)


def _canonical_env_name(name: str) -> str:
    return name.replace("MINIGENT_", "MINDWEFT_", 1)


def _positive_int_env(name: str, default: int) -> int:
    raw = preferred_mindweft_env(name.removeprefix("MINIGENT_"))
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_canonical_env_name(name)} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{_canonical_env_name(name)} must be a positive integer")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = preferred_mindweft_env(name.removeprefix("MINIGENT_"))
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_canonical_env_name(name)} must be positive") from exc
    if value <= 0:
        raise RuntimeError(f"{_canonical_env_name(name)} must be positive")
    return value
