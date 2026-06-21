import pytest

from app.tenants import (
    TenantRegistrySettings,
    tenant_registry_required_from_env,
    tenant_registry_settings_from_env,
    tenant_user_registry_required_from_env,
)


def test_tenant_registry_settings_from_env_mapping_uses_defaults() -> None:
    assert TenantRegistrySettings.from_env({}) == TenantRegistrySettings(
        tenant_registry_required=False,
        tenant_user_registry_required=False,
    )


def test_tenant_registry_settings_from_env_mapping_parses_values() -> None:
    settings = TenantRegistrySettings.from_env(
        {
            "MINIGENT_TENANT_REGISTRY_REQUIRED": "yes",
            "MINIGENT_TENANT_USER_REGISTRY_REQUIRED": "on",
        }
    )

    assert settings == TenantRegistrySettings(
        tenant_registry_required=True,
        tenant_user_registry_required=True,
    )


def test_tenant_registry_settings_from_env_mapping_accepts_false_values() -> None:
    settings = TenantRegistrySettings.from_env(
        {
            "MINIGENT_TENANT_REGISTRY_REQUIRED": "0",
            "MINIGENT_TENANT_USER_REGISTRY_REQUIRED": "false",
        }
    )

    assert settings == TenantRegistrySettings(
        tenant_registry_required=False,
        tenant_user_registry_required=False,
    )


def test_tenant_registry_settings_from_env_mapping_rejects_invalid_values() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        TenantRegistrySettings.from_env({"MINIGENT_TENANT_REGISTRY_REQUIRED": "sometimes"})

    assert str(exc_info.value) == "MINIGENT_TENANT_REGISTRY_REQUIRED must be a boolean"


def test_tenant_registry_settings_from_env_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    monkeypatch.setenv("MINIGENT_TENANT_USER_REGISTRY_REQUIRED", "false")

    assert tenant_registry_settings_from_env() == TenantRegistrySettings(
        tenant_registry_required=True,
        tenant_user_registry_required=False,
    )
    assert tenant_registry_required_from_env() is True
    assert tenant_user_registry_required_from_env() is False
