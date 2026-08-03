from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit

import httpx

from app.mcp_identity import MCPIdentityTokenIssuer

EXTERNAL_GRANT_PROVIDERS_ENV = "MINIGENT_ADMIN_EXTERNAL_GRANT_PROVIDERS"
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ExternalGrantProviderError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ExternalGrant:
    resource_id: str
    subject_id: str
    permission: str
    enabled: bool
    updated_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ExternalGrantAuditState:
    permission: str
    enabled: bool


@dataclass(frozen=True)
class ExternalGrantAudit:
    audit_id: int
    resource_id: str
    subject_id: str
    actor_id: str
    operation: str
    previous: ExternalGrantAuditState | None
    resulting: ExternalGrantAuditState | None
    created_at: str


@dataclass(frozen=True, repr=False)
class HTTPExternalGrantProvider:
    provider_id: str
    title: str
    description: str
    base_url: str
    list_path: str
    upsert_path: str
    delete_path: str
    allowed_permissions: tuple[str, ...]
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    token_issuer: MCPIdentityTokenIssuer
    audit_path: str | None = None
    timeout_seconds: float = 10.0

    async def list_grants(self, *, tenant_id: str, actor_user_id: str) -> list[ExternalGrant]:
        payload = await self._request(
            "GET",
            self.list_path,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scopes=self.read_scopes,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("grants"), list):
            raise ExternalGrantProviderError(
                502, "Grant provider returned an invalid list response"
            )
        return [_parse_grant(item, self.allowed_permissions) for item in payload["grants"]]

    async def upsert_grant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        resource_id: str,
        subject_id: str,
        permission: str,
        enabled: bool,
    ) -> ExternalGrant:
        if permission not in self.allowed_permissions:
            raise ExternalGrantProviderError(422, f"Unsupported grant permission '{permission}'")
        payload = await self._request(
            "PUT",
            self.upsert_path,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scopes=self.write_scopes,
            json_body={
                "resource_id": resource_id,
                "user_id": subject_id,
                "permission": permission,
                "enabled": enabled,
            },
        )
        return _parse_grant(payload, self.allowed_permissions)

    async def delete_grant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        resource_id: str,
        subject_id: str,
    ) -> None:
        path = self.delete_path.replace("{resource_id}", quote(resource_id, safe=""))
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}{urlencode({'user_id': subject_id})}"
        await self._request(
            "DELETE",
            path,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scopes=self.write_scopes,
            expect_json=False,
        )

    async def list_audit(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        limit: int,
        before_id: int | None = None,
    ) -> tuple[list[ExternalGrantAudit], int | None]:
        if self.audit_path is None:
            raise ExternalGrantProviderError(404, "Grant provider does not expose audit history")
        separator = "&" if "?" in self.audit_path else "?"
        query: dict[str, int] = {"limit": limit}
        if before_id is not None:
            query["before_id"] = before_id
        payload = await self._request(
            "GET",
            f"{self.audit_path}{separator}{urlencode(query)}",
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            scopes=self.read_scopes,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise ExternalGrantProviderError(
                502, "Grant provider returned an invalid audit response"
            )
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, int) or next_cursor < 1):
            raise ExternalGrantProviderError(
                502, "Grant provider returned an invalid audit response"
            )
        return ([_parse_grant_audit(item) for item in payload["entries"]], next_cursor)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        actor_user_id: str,
        scopes: tuple[str, ...],
        json_body: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        token = self.token_issuer.issue(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            scopes=scopes,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(
                    method,
                    self.base_url + path,
                    headers={"Authorization": f"Bearer {token}"},
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise ExternalGrantProviderError(503, "Grant provider is unavailable") from exc
        if response.status_code >= 400:
            detail = _provider_error_detail(response)
            status_code = response.status_code if response.status_code in {404, 409, 422} else 502
            raise ExternalGrantProviderError(status_code, detail)
        if not expect_json:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ExternalGrantProviderError(502, "Grant provider returned invalid JSON") from exc


class ExternalGrantProviderRegistry:
    def __init__(self, providers: list[HTTPExternalGrantProvider] | None = None) -> None:
        self._providers = {provider.provider_id: provider for provider in providers or []}

    def list(self) -> list[HTTPExternalGrantProvider]:
        return sorted(self._providers.values(), key=lambda provider: provider.provider_id)

    def get(self, provider_id: str) -> HTTPExternalGrantProvider | None:
        return self._providers.get(provider_id)


def build_external_grant_provider_registry_from_env(
    env: Mapping[str, str] | None = None,
) -> ExternalGrantProviderRegistry:
    lookup = os.environ if env is None else env
    raw = lookup.get(EXTERNAL_GRANT_PROVIDERS_ENV, "").strip()
    if not raw:
        return ExternalGrantProviderRegistry()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{EXTERNAL_GRANT_PROVIDERS_ENV} must be valid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"{EXTERNAL_GRANT_PROVIDERS_ENV} must be a JSON array")
    providers: list[HTTPExternalGrantProvider] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise RuntimeError(f"Grant provider entry {index} must be an object")
        provider_id = _required_string(item, "id", index)
        if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
            raise RuntimeError(f"Grant provider entry {index} has an invalid id")
        if provider_id in seen:
            raise RuntimeError(f"Grant provider id '{provider_id}' is duplicated")
        seen.add(provider_id)
        base_url = _validate_base_url(_required_string(item, "base_url", index), index)
        audience = _required_string(item, "audience", index)
        permissions = _string_tuple(item.get("allowed_permissions", ["read", "read_write"]), index)
        read_scopes = _string_tuple(item.get("read_scopes"), index)
        write_scopes = _string_tuple(item.get("write_scopes"), index)
        list_path = _path(item.get("list_path", "/v1/resource-grants"), "list_path", index)
        upsert_path = _path(item.get("upsert_path", list_path), "upsert_path", index)
        delete_path = _path(
            item.get("delete_path", "/v1/resource-grants/{resource_id}"),
            "delete_path",
            index,
        )
        audit_path_value = item.get("audit_path")
        audit_path = (
            _path(audit_path_value, "audit_path", index) if audit_path_value is not None else None
        )
        if "{resource_id}" not in delete_path:
            raise RuntimeError(f"Grant provider entry {index} delete_path requires {{resource_id}}")
        providers.append(
            HTTPExternalGrantProvider(
                provider_id=provider_id,
                title=_required_string(item, "title", index),
                description=str(item.get("description", "")).strip(),
                base_url=base_url,
                list_path=list_path,
                upsert_path=upsert_path,
                delete_path=delete_path,
                allowed_permissions=permissions,
                read_scopes=read_scopes,
                write_scopes=write_scopes,
                token_issuer=MCPIdentityTokenIssuer.from_env(audience=audience, env=lookup),
                audit_path=audit_path,
                timeout_seconds=_timeout_seconds(item.get("timeout_seconds", 10.0), index),
            )
        )
    return ExternalGrantProviderRegistry(providers)


def _parse_grant(value: Any, allowed_permissions: tuple[str, ...]) -> ExternalGrant:
    if not isinstance(value, dict):
        raise ExternalGrantProviderError(502, "Grant provider returned an invalid grant")
    resource_id = value.get("resource_id")
    subject_id = value.get("user_id", value.get("subject_id"))
    permission = value.get("permission")
    enabled = value.get("enabled")
    if (
        not isinstance(resource_id, str)
        or not resource_id
        or not isinstance(subject_id, str)
        or not subject_id
        or not isinstance(permission, str)
        or permission not in allowed_permissions
        or not isinstance(enabled, bool)
    ):
        raise ExternalGrantProviderError(502, "Grant provider returned an invalid grant")
    return ExternalGrant(
        resource_id=resource_id,
        subject_id=subject_id,
        permission=permission,
        enabled=enabled,
        updated_by=str(value["updated_by"]) if value.get("updated_by") is not None else None,
        created_at=str(value["created_at"]) if value.get("created_at") is not None else None,
        updated_at=str(value["updated_at"]) if value.get("updated_at") is not None else None,
    )


def _parse_grant_audit(value: Any) -> ExternalGrantAudit:
    if not isinstance(value, dict):
        raise ExternalGrantProviderError(502, "Grant provider returned an invalid audit entry")
    audit_id = value.get("audit_id")
    resource_id = value.get("resource_id")
    subject_id = value.get("user_id", value.get("subject_id"))
    actor_id = value.get("actor_id")
    operation = value.get("operation")
    created_at = value.get("created_at")
    if (
        not isinstance(audit_id, int)
        or audit_id < 1
        or not isinstance(resource_id, str)
        or not resource_id
        or not isinstance(subject_id, str)
        or not subject_id
        or not isinstance(actor_id, str)
        or not actor_id
        or not isinstance(operation, str)
        or not operation
        or not isinstance(created_at, str)
        or not created_at
    ):
        raise ExternalGrantProviderError(502, "Grant provider returned an invalid audit entry")
    return ExternalGrantAudit(
        audit_id=audit_id,
        resource_id=resource_id,
        subject_id=subject_id,
        actor_id=actor_id,
        operation=operation,
        previous=_parse_grant_audit_state(value.get("previous")),
        resulting=_parse_grant_audit_state(value.get("resulting")),
        created_at=created_at,
    )


def _parse_grant_audit_state(value: Any) -> ExternalGrantAuditState | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ExternalGrantProviderError(502, "Grant provider returned an invalid audit entry")
    permission = value.get("permission")
    enabled = value.get("enabled")
    if not isinstance(permission, str) or not permission or not isinstance(enabled, bool):
        raise ExternalGrantProviderError(502, "Grant provider returned an invalid audit entry")
    return ExternalGrantAuditState(permission=permission, enabled=enabled)


def _required_string(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Grant provider entry {index} requires {key}")
    return value.strip()


def _string_tuple(value: Any, index: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise RuntimeError(f"Grant provider entry {index} requires a non-empty string list")
    return tuple(dict.fromkeys(value))


def _path(value: Any, key: str, index: int) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise RuntimeError(f"Grant provider entry {index} has an invalid {key}")
    return value


def _validate_base_url(value: str, index: int) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"Grant provider entry {index} has an invalid base_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(
            f"Grant provider entry {index} base_url must not contain credentials or query data"
        )
    return value.rstrip("/")


def _timeout_seconds(value: Any, index: int) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Grant provider entry {index} has an invalid timeout_seconds") from exc
    if not 1.0 <= timeout <= 60.0:
        raise RuntimeError(f"Grant provider entry {index} timeout_seconds must be between 1 and 60")
    return timeout


def _provider_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "Grant provider rejected the request"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"]
        if isinstance(detail, str):
            return detail
    return "Grant provider rejected the request"
